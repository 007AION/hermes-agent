"""Isolated RED→GREEN tests for AION-RL2-CORE-01-R15 terminal run reconciliation.

The live board carries three task_runs rows that are still ``running`` with
``ended_at IS NULL`` although their parent tasks are ``done`` and their
``current_run_id`` is NULL: legacy run 664 (t_8e8e8d62) and the synthetic R12
probe runs 2056 (t_c0093dec) / 2061 (t_bafab551). Board diagnostics (R12)
correctly exclude those orphan rows from ``executable_now``, but the run
*ledger* is never reconciled: the terminal transition (``complete_task``)
closes only the run pointed to by ``current_run_id`` (via ``_end_run``) and,
when that pointer is NULL, merely *synthesizes* a fresh completion run —
leaving the already-open orphan row open forever.

R15 closes that gap at the terminal transition itself, so the invariant

    ``current_run_id IS NULL``  ⇔  no ``task_runs`` row with ``ended_at IS NULL``

is durable for every terminal task, without a new GC daemon / reconciler /
control plane and without touching a distinct live worker or run.

Contract under test:

* A terminal task must never coexist with an open (``ended_at IS NULL``) run
  row for the *same* task, regardless of how the orphan arose (legacy code,
  an external/synthetic write, a crash/give_up sequence, or any un-traced
  path).
* The reconciliation is scoped to the *exact owned* task and never closes a
  distinct live worker's run (fail-closed).
* The existing ``current_run_id`` / CAS / fencing semantics are preserved:
  the normal worker-bound completion path is unchanged, already-terminal
  completion is idempotent, and a single completion never produces a second
  ``completed`` run.

Tests are hermetic under a dispatcher-pinned environment (see ``kanban_home``).

File: tests/hermes_cli/test_kanban_terminal_run_reconciliation_r15.py
"""

from __future__ import annotations

import time
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


def _claim_then_detach(conn, title, *, assignee="a"):
    """Create + claim a task, then detach the pointer while the run stays open.

    Reproduces the invariant leak at the root of the live board's three orphan
    rows: an owned run is left ``running``/``ended_at IS NULL`` while the
    task's ``current_run_id`` is cleared by an external / legacy write. This
    is the *historical* entry path — the current code pairs every run create
    with ``current_run_id`` and every close with a pointer clear, so a fresh
    orphan cannot be produced through the public API alone (see module docstring).
    """
    t = kb.create_task(conn, title=title, assignee=assignee)
    kb.claim_task(conn, t)
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (t,),
    )
    conn.commit()
    return t


# ── RED: terminal completion must reconcile an orphan open run ────────────────


def test_r15_terminal_completion_reconciles_orphan_open_run(kanban_home):
    """RED: completing a task with a detached open run must close that run."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "orphan-owner")
        assert len(_open_runs(conn, t)) == 1  # the leak is in place

        kb.complete_task(conn, t, summary="closed as superseded")

        orphans = _open_runs(conn, t)
        task_status = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert task_status["status"] == "done"
    assert task_status["current_run_id"] is None
    assert orphans == []  # RED: the orphan is still open on the current base


# ── GREEN: crash/give_up then later terminal completion ───────────────────────


def _runs_by_outcome(conn, task_id):
    rows = conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM task_runs WHERE task_id = ? "
        "GROUP BY outcome",
        (task_id,),
    ).fetchall()
    return {r["outcome"]: int(r["n"]) for r in rows}


def test_r15_crash_give_up_then_terminal_completion_reconciles(kanban_home):
    """A crash that leaves an open run + a gave_up block is reconciled on
    completion: the terminal task has no open run and exactly one completed
    run."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "crashed-then-done")
        # Breaker-tripped gave_up parks the task in ``blocked`` while the run
        # stays orphaned (the R12 probe residue shape).
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.commit()

        assert len(_open_runs(conn, t)) == 1

        kb.complete_task(conn, t, summary="reconciled after gave_up")

        orphans = _open_runs(conn, t)
        outcomes = _runs_by_outcome(conn, t)
        status = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert status["status"] == "done"
    assert status["current_run_id"] is None
    assert orphans == []
    assert outcomes.get("completed") == 1


def test_r15_externally_initiated_terminalization_reconciles(kanban_home):
    """Controller/CLI terminalization (no worker summary) still closes the
    orphan open run without fabricating a completion run."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "external-terminalize")

        kb.complete_task(conn, t)  # external, no summary/result

        orphans = _open_runs(conn, t)
        outcomes = _runs_by_outcome(conn, t)
        status = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (t,),
        ).fetchone()

    assert status["status"] == "done"
    assert orphans == []
    # No summary → no synthesized completion run; the orphan was closed as
    # ``reclaimed``, not ``completed``.
    assert outcomes.get("completed", 0) == 0
    assert outcomes.get("reclaimed") == 1


def test_r15_already_terminal_completion_is_idempotent(kanban_home):
    """Completing an already-done task is a no-op: no new run, no state drift."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="already-done", assignee="a")
        kb.claim_task(conn, t)
        kb.complete_task(conn, t, summary="first")

        before_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (t,),
        ).fetchone()[0]
        ret = kb.complete_task(conn, t, summary="second")

        after_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (t,),
        ).fetchone()[0]
        open_count = len(_open_runs(conn, t))

    assert ret is False
    assert after_runs == before_runs
    assert open_count == 0


def test_r15_distinct_live_run_is_fail_closed(kanban_home):
    """Reconciling task A never touches task B's live run."""
    with kb.connect() as conn:
        a = _claim_then_detach(conn, "reconcile-me")
        b = kb.create_task(conn, title="live-worker", assignee="a")
        kb.claim_task(conn, b)  # B: live run, current_run_id set

        kb.complete_task(conn, a, summary="closed")

        a_orphans = _open_runs(conn, a)
        b_open = _open_runs(conn, b)
        b_state = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (b,),
        ).fetchone()

    assert a_orphans == []
    assert len(b_open) == 1  # B's live run is untouched
    assert b_state["status"] == "running"
    assert b_state["current_run_id"] is not None


def test_r15_no_duplicate_completion_run(kanban_home):
    """One completion yields exactly one ``completed`` run — the orphan is
    closed as ``reclaimed`` and the synthesize fallback is not doubled."""
    with kb.connect() as conn:
        t = _claim_then_detach(conn, "single-completion")
        assert len(_open_runs(conn, t)) == 1

        kb.complete_task(conn, t, summary="done once")

        outcomes = _runs_by_outcome(conn, t)
        open_count = len(_open_runs(conn, t))

    assert open_count == 0
    assert outcomes.get("completed") == 1  # not two
    assert outcomes.get("reclaimed") == 1  # the orphan

