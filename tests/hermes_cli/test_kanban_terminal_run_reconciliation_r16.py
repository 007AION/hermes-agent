"""Isolated RED→GREEN tests for AION-RL2-CORE-01-R16 archive + exact residue closure.

R15 (PR #12, merged main 603f9ecc) closed the terminal invariant at
``complete_task``. The independent audit then proved two explicit remaining
gaps, which this change closes with the smallest existing-path delta:

1. **``archive_task`` still leaves a detached open run.** It calls ``_end_run``
   (closing only the run pointed to by ``current_run_id``) but *not*
   ``_close_orphan_open_runs``. A task whose run was orphaned by a legacy /
   external write (pointer already detached, ``current_run_id IS NULL``) is
   therefore archived with its run row still ``ended_at IS NULL`` — the same
   invariant breach R15 fixed for completion, but on the other terminal path.

2. **Already-terminal residues have no supported exact repair path.** The
   live board carries three ``done`` tasks (t_8e8e8d62 → run 664,
   t_c0093dec → run 2056, t_bafab551 → run 2061) whose run rows are still
   ``running``/``ended_at IS NULL``. The only reconciliation lives *inside*
   the terminal transition, so an *already* terminal task cannot have its open
   run rows closed without a raw DB write (forbidden). R16 adds one bounded,
   supported exact-task operation — ``repair_terminal_orphan_runs`` — that
   closes open run rows for a single named already-terminal task, refuses
   nonterminal / currently-owned tasks, is idempotent, and returns a
   deterministic machine-readable receipt for later exact live-row readback.

The invariant under test is unchanged from R15:

    ``current_run_id IS NULL``  ⇔  no ``task_runs`` row with ``ended_at IS NULL``

now durable for **both** terminal transitions (``complete_task`` *and*
``archive_task``) *and* repairable for tasks that are already terminal, without
a GC daemon / reconciler / second control plane and without ever touching a
distinct live worker's run.

Contract under test:

* ``archive_task`` reconciles a detached orphan open run for the exact archived
  task (same transaction / idempotency / CAS / fencing discipline as R15).
* ``repair_terminal_orphan_runs(task_id)``:
    - repairs **one** named task per call (no broad scan, exact
      ``WHERE task_id = ?`` scoping);
    - requires an already-terminal task (``done`` / ``archived`` — *not*
      ``blocked``, which may be unblocked and re-run);
    - requires no live current-run ownership (``current_run_id IS NULL``);
    - is idempotent (second call is a no-op, not a duplicate);
    - returns a deterministic receipt carrying exact before/after run ids and
      outcomes suitable for later exact live-row readback;
    - fails closed: never closes a distinct live worker's run.
* No terminal transition or repair fabricates a second ``completed`` run.

File: tests/hermes_cli/test_kanban_terminal_run_reconciliation_r16.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + pinned Native Kanban DB (no live-board leak)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.isolated_kanban_env(home):
        kb.init_db()
        yield home


def _open_runs(conn, task_id):
    return conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NULL "
        "ORDER BY id",
        (task_id,),
    ).fetchall()


def _open_run_ids(conn, task_id):
    return [int(r["id"]) for r in _open_runs(conn, task_id)]


def _claim_then_detach(conn, title, *, assignee="a"):
    """Create + claim a task, then detach the pointer and release ownership
    while the run stays open.

    Reproduces the *safe* historical residue shape at the root of the live
    board's three orphan rows (664 / 2056 / 2061): an owned run is left
    ``running`` / ``ended_at IS NULL`` while the task's ``current_run_id`` is
    cleared *and its claim/worker ownership evidence is released* by an
    external / legacy write. This is the shape the terminal-residue repair is
    allowed to close; a residue that still carries a claim lock, unexpired
    claim expiry, or worker PID is the *ambiguous* shape the repair must
    refuse (see the adversarial tests below). (AION-RL2-CORE-01-R16 audit.)
    """
    t = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, t)
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL, claim_lock = NULL, "
        "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
        (t,),
    )
    conn.execute(
        "UPDATE task_runs SET claim_lock = NULL, claim_expires = NULL, "
        "worker_pid = NULL WHERE task_id = ?",
        (t,),
    )
    conn.commit()
    return t


def _runs_by_outcome(conn, task_id):
    rows = conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM task_runs WHERE task_id = ? "
        "GROUP BY outcome",
        (task_id,),
    ).fetchall()
    return {r["outcome"]: int(r["n"]) for r in rows}


# ── RED: archive must reconcile an orphan open run ───────────────────────────


def test_r16_archive_reconciles_orphan_open_run(kanban_home):
    """RED: archiving a task with a detached open run must close that run."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "archive-orphan-owner")
        assert _open_run_ids(conn, t) != []  # the leak is in place

        kb.archive_task(conn, t)

        orphans = _open_run_ids(conn, t)
        outcomes = _runs_by_outcome(conn, t)
        task_status = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert task_status["status"] == "archived"
    assert task_status["current_run_id"] is None
    assert orphans == []  # RED: the orphan is still open on the current base
    assert outcomes.get("reclaimed") == 1  # closed as reclaimed, not completed


def test_r16_archive_does_not_touch_distinct_live_run(kanban_home):
    """Archiving task A never touches task B's live run (fail-closed)."""
    with kb.connect() as conn:
        a = _claim_then_detach(conn, "archive-me")
        b = kb.create_task(conn, title="live-worker", assignee="a")
        kb.claim_task(conn, b)  # B: live run, current_run_id set

        kb.archive_task(conn, a)

        a_orphans = _open_run_ids(conn, a)
        b_open = _open_run_ids(conn, b)
        b_state = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (b,),
        ).fetchone()

    assert a_orphans == []
    assert b_open == [b_state["current_run_id"]]  # B's live run untouched
    assert b_state["status"] == "running"
    assert b_state["current_run_id"] is not None


# ── GREEN: bounded exact-task terminal-run repair ────────────────────────────


def test_r16_repair_terminal_orphan_run(kanban_home):
    """Repair closes an open run on an already-terminal task and returns a
    deterministic receipt with exact before/after run ids."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "terminal-residue")
        # Terminalize like the live residues: done + current_run_id NULL.
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        conn.commit()
        before = _open_run_ids(conn, t)
        assert len(before) == 1

        receipt = kb.repair_terminal_orphan_runs(conn, t)

        after = _open_run_ids(conn, t)
        outcomes = _runs_by_outcome(conn, t)

    assert receipt["task_id"] == t
    assert receipt["task_status"] == "done"
    assert receipt["refused"] is None
    assert receipt["repaired"] is True
    assert receipt["before_open_run_ids"] == before
    assert receipt["closed_run_ids"] == before
    assert receipt["after_open_run_ids"] == []
    assert after == []
    assert outcomes.get("reclaimed") == 1  # repaired as reclaimed


def test_r16_repair_is_idempotent(kanban_home):
    """A second repair on an already-repaired task is a no-op, not a duplicate."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "repair-idempotent")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        conn.commit()

        first = kb.repair_terminal_orphan_runs(conn, t)
        before_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (t,),
        ).fetchone()[0]

        second = kb.repair_terminal_orphan_runs(conn, t)
        after_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (t,),
        ).fetchone()[0]

    assert first["repaired"] is True
    assert second["refused"] is None
    assert second["repaired"] is False
    assert second["before_open_run_ids"] == []
    assert second["closed_run_ids"] == []
    assert second["after_open_run_ids"] == []
    assert after_runs == before_runs  # no duplicate run row


def test_r16_repair_refuses_nonterminal_task(kanban_home):
    """A ready (non-terminal) task is refused — its open-run semantics are not
    a closed-loop residue yet."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="still-ready", assignee="a")
        receipt = kb.repair_terminal_orphan_runs(conn, t)
        status = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert receipt["refused"] == "nonterminal_task"
    assert receipt["repaired"] is False
    assert status["status"] == "ready"  # untouched


def test_r16_repair_refuses_live_current_run(kanban_home):
    """A terminal-status task that still owns a live run is refused (fail
    closed) — never close a live worker's run."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="terminal-but-owned", assignee="a")
        kb.claim_task(conn, t)  # current_run_id set, run live
        # Anomalous terminal+owned shape: flip status but keep the pointer.
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        conn.commit()

        receipt = kb.repair_terminal_orphan_runs(conn, t)
        still_open = _open_run_ids(conn, t)

    assert receipt["refused"] == "live_current_run"
    assert receipt["repaired"] is False
    assert still_open != []  # the live run was NOT closed


def test_r16_repair_unknown_task(kanban_home):
    """An unknown task id is refused deterministically, not an error."""
    with kb.connect() as conn:
        receipt = kb.repair_terminal_orphan_runs(conn, "t_does_not_exist")
    assert receipt["refused"] == "unknown_task"
    assert receipt["repaired"] is False


def test_r16_repair_does_not_touch_distinct_live_run(kanban_home):
    """Repairing task A never touches task B's live run (exact-task scoping)."""
    with kb.connect() as conn:
        a = _claim_then_detach(conn, "repair-me")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
        conn.commit()
        b = kb.create_task(conn, title="live-worker", assignee="a")
        kb.claim_task(conn, b)  # B: live run, current_run_id set

        receipt = kb.repair_terminal_orphan_runs(conn, a)

        a_orphans = _open_run_ids(conn, a)
        b_open = _open_run_ids(conn, b)
        b_state = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (b,),
        ).fetchone()

    assert receipt["repaired"] is True
    assert a_orphans == []
    assert b_open == [b_state["current_run_id"]]
    assert b_state["status"] == "running"
    assert b_state["current_run_id"] is not None


def test_r16_archive_no_duplicate_completion_run(kanban_home):
    """Archiving an orphan-owner yields exactly one reclaimed run, never a
    fabricated ``completed`` run."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "archive-single-run")
        assert len(_open_run_ids(conn, t)) == 1

        kb.archive_task(conn, t)

        outcomes = _runs_by_outcome(conn, t)
        open_count = len(_open_run_ids(conn, t))

    assert open_count == 0
    assert outcomes.get("reclaimed") == 1
    assert outcomes.get("completed", 0) == 0  # not doubled, not fabricated


def test_r16_reconcile_cli_receipt(kanban_home):
    """The ``reconcile-terminal-runs`` CLI command repairs the exact named task
    end-to-end and emits a deterministic JSON receipt (stdout line 1)."""
    from hermes_cli import kanban as kc

    with kb.connect() as conn:
        t = _claim_then_detach(conn, "cli-repair")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        conn.commit()

    out = kc.run_slash(f"reconcile-terminal-runs {t}")

    # stdout is the JSON receipt; stderr carries the human confirmation line,
    # so parse the first line.
    receipt = json.loads(out.splitlines()[0])

    with kb.connect() as conn:
        open_ids = _open_run_ids(conn, t)

    assert receipt["task_id"] == t
    assert receipt["refused"] is None
    assert receipt["repaired"] is True
    assert receipt["closed_count"] == 1
    assert receipt["after_open_run_ids"] == []
    assert open_ids == []


# ── RED: fail closed on ambiguous live ownership (AION-RL2-CORE-01-R16 audit) ─


def test_r16_repair_refuses_ambiguous_detached_ownership(kanban_home):
    """RED: a terminal task whose pointer is detached but which still carries
    non-null claim lock / unexpired claim expiry / worker PID on BOTH task and
    run must be refused without partial mutation (the auditor's exact shape)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="detached-but-owned", assignee="a")
        kb.claim_task(conn, t)  # sets claim_lock + unexpired claim_expires
        # Adversarial terminal+detached+owned shape: flip status, clear the
        # pointer, but KEEP claim lock / unexpired expiry and set worker PID.
        conn.execute(
            "UPDATE tasks SET status = 'done', current_run_id = NULL, "
            "worker_pid = 4242 WHERE id = ?",
            (t,),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = 4242 WHERE task_id = ?", (t,),
        )
        conn.commit()

        before_task = conn.execute(
            "SELECT claim_lock, claim_expires, worker_pid, current_run_id "
            "FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        before_run = conn.execute(
            "SELECT claim_lock, claim_expires, worker_pid, ended_at "
            "FROM task_runs WHERE task_id = ?",
            (t,),
        ).fetchone()

        receipt = kb.repair_terminal_orphan_runs(conn, t)

        after_task = conn.execute(
            "SELECT claim_lock, claim_expires, worker_pid, current_run_id "
            "FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        after_run = conn.execute(
            "SELECT claim_lock, claim_expires, worker_pid, ended_at "
            "FROM task_runs WHERE task_id = ?",
            (t,),
        ).fetchone()

    assert receipt["refused"] == "ambiguous_live_ownership"
    assert receipt["repaired"] is False
    assert receipt["closed_count"] == 0
    # No partial mutation: ownership evidence and the open run are untouched.
    assert after_task["claim_lock"] == before_task["claim_lock"]
    assert after_task["claim_expires"] == before_task["claim_expires"]
    assert after_task["worker_pid"] == before_task["worker_pid"]
    assert after_task["current_run_id"] is None
    assert after_run["claim_lock"] == before_run["claim_lock"]
    assert after_run["worker_pid"] == before_run["worker_pid"]
    assert after_run["ended_at"] is None  # the open run was NOT closed


def test_r16_repair_refuses_run_level_ambiguous_ownership(kanban_home):
    """RED: a clean task row but an open run row that still carries claim lock
    / worker PID must be refused (run-level fence, not just task-level)."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "run-level-owned")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        # Re-introduce ambiguous ownership on the RUN row only.
        conn.execute(
            "UPDATE task_runs SET claim_lock = 'host:9', "
            "claim_expires = 9999999999, worker_pid = 7777 WHERE task_id = ?",
            (t,),
        )
        conn.commit()

        receipt = kb.repair_terminal_orphan_runs(conn, t)
        still_open = _open_run_ids(conn, t)

    assert receipt["refused"] == "ambiguous_live_ownership"
    assert receipt["repaired"] is False
    assert still_open != []  # not closed


def test_r16_repair_allows_expired_claim_expiry(kanban_home):
    """An *expired* claim (claim_lock/worker_pid already NULL, claim_expires in
    the past) is NOT ambiguous and may be closed — the fence is precise."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "expired-claim")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        # Stale, already-expired claim timestamp with no lock / worker.
        conn.execute(
            "UPDATE task_runs SET claim_expires = 1 WHERE task_id = ?", (t,),
        )
        conn.commit()

        receipt = kb.repair_terminal_orphan_runs(conn, t)

    assert receipt["refused"] is None
    assert receipt["repaired"] is True


# ── RED: deterministic per-row receipt + bounded outcome (AION-RL2-CORE-01-R16) ─


def test_r16_repair_receipt_discloses_per_row_before_after(kanban_home):
    """RED: the receipt must disclose each targeted row's exact before/after
    status and outcome plus closure evidence (not just run ids/counts)."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "receipt-rows")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        conn.commit()

        receipt = kb.repair_terminal_orphan_runs(conn, t)

    assert receipt["repaired"] is True
    rows = receipt["rows"]
    assert len(rows) == len(receipt["closed_run_ids"])
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == receipt["closed_run_ids"][0]
    assert row["before"]["status"] == "running"
    assert row["before"]["outcome"] is None
    assert row["after"]["status"] == "reclaimed"
    assert row["after"]["outcome"] == "reclaimed"
    assert row["closure"]["evidence"]
    assert isinstance(row["closure"]["ended_at"], int)
    # Stable ordering: rows ascending by run_id.
    assert [r["run_id"] for r in rows] == sorted(r["run_id"] for r in rows)


def test_r16_repair_outcome_is_not_injectable(kanban_home):
    """RED: the repair outcome is fixed — an arbitrary ``outcome=`` kwarg is
    rejected, so a caller cannot mark a historical row ``completed``."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "outcome-injection")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t,))
        conn.commit()

        with pytest.raises(TypeError):
            kb.repair_terminal_orphan_runs(conn, t, outcome="completed")

    with kb.connect() as conn:
        outcomes = _runs_by_outcome(conn, t)

    # The row was never marked completed.
    assert outcomes.get("completed", 0) == 0
