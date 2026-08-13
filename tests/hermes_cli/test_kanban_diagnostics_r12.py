"""Isolated RED→GREEN tests for AION-RL2-CORE-01-R12 board diagnostics repair.

The installed ``compute_board_diagnostics`` undercounted open obligations
because its static ``_NONTERMINAL_STATUSES`` omitted ``fenced`` (a live
nonterminal lifecycle), and it defined ``executable_now`` as dispatchable
``ready`` only — so ``executable_now=0`` was confused with "no unfinished
work" even when active/reclaimable running work was in flight.

The R12 repair (PR #10) fixed the terminal projection and decomposed
``executable_now``. The bafuxunan exact-head audit then returned
REQUEST_CHANGES on four safety blockers, which these tests now prove fixed:

1. A stale-heartbeat expired live worker was falsely projected
   ``reclaimable`` even though ``release_stale_claims`` would *terminate*
   it and that termination may survive → a deferred hold, not a reclaim.
2. ``worker_starttime`` / spawned-event PID identity was ignored, so a
   PID-recycled impostor read as a live worker.
3. Authoritative ``current_run_id`` ownership was ignored.
4. Adversarial coverage (termination survival, PID reuse, stale/NULL
   ownership, missing identity, TTL boundary) was absent.

Contract under test:

* ``done`` / ``archived`` are the ONLY terminal exclusions; every other
  current or future status is fail-closed as an open obligation.
* ``executable_now`` = dispatchable ready + active running + safely
  reclaimable running, never fenced wait states and never fail-closed
  ``deferred`` running work, with no double counting.
* ``reclaimable_running`` is machine-proven safe/executable *now* (dead
  host-local PID, expired foreign claim, or expired PID-less claim), never
  a candidate for a potentially deferred reclaim.
* ``deferred_running`` (fail-closed) is neither provably in flight nor
  provably reclaimable: stale-heartbeat termination candidates,
  PID-recycled/missing-identity workers, missing/stale ``current_run_id``.

Tests are hermetic under a dispatcher-pinned environment: ``kanban_home``
binds every Native Kanban path/board pin to the temp root and clears the
worker-identity pins, so ``kb.connect()`` can never write synthetic rows
into a live board.

File: tests/hermes_cli/test_kanban_diagnostics_r12.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


_HOST_PREFIX = f"{kb._claimer_id().split(':', 1)[0]}:"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + pinned Native Kanban DB (no live-board leak)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Hermetic isolation under a dispatcher-pinned environment: rebind every
    # Native Kanban path/board pin to the temp root and clear worker-identity
    # pins. `isolated_kanban_env` is the canonical, audited helper for this —
    # it pins HERMES_KANBAN_DB to <home>/kanban.db and clears board selectors,
    # so kb.connect() resolves here instead of an inherited live board.
    with kb.isolated_kanban_env(home):
        kb.init_db()
        yield home


def _spawn_worker(conn, task_id, *, pid, starttime):
    """Stamp ``worker_pid`` and a ``spawned`` event carrying the starttime.

    Mirrors ``_set_worker_pid``: the ``spawned`` event payload's ``starttime``
    is the authoritative PID identity anchor for a running worker.
    """
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()["current_run_id"]
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, task_id))
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, 'spawned', ?, ?)",
        (task_id, run_id, json.dumps({"pid": pid, "starttime": starttime}),
         int(time.time())),
    )
    conn.commit()


def _claim_running(conn, title, *, pid=None, expires=None):
    """Create + claim a task, then stamp worker_pid / claim_expires directly."""
    t = kb.create_task(conn, title=title, assignee="a")
    kb.claim_task(conn, t)
    if pid is not None:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, t))
    if expires is not None:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?", (expires, t)
        )
    conn.commit()
    return t


def _live_worker(monkeypatch, *, starttime=222333):
    """Patch liveness + identity so a host-local PID reads as the same worker."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        kb,
        "_read_process_identity",
        lambda pid: {"starttime": starttime, "cwd": "/ws", "pgid": 1},
    )


def _make_fenced(conn, assignee="a"):
    """Park a task in ``fenced`` (nonterminal wait state, never executable)."""
    t = kb.create_task(conn, title="fenced-wait", assignee=assignee)
    conn.execute(
        "UPDATE tasks SET status = 'fenced', worker_pid = 424242, "
        "worker_starttime = 111222 WHERE id = ?",
        (t,),
    )
    conn.commit()
    return t


# ── RED / GREEN: open obligations are fail-closed ────────────────────────────


def test_r12_open_obligations_counts_fenced_and_blocked(kanban_home):
    """blocked=1 + fenced=1 yields open_obligations=2 (not the old 1)."""
    with kb.connect() as conn:
        blocked = kb.create_task(conn, title="blocked", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (blocked,)
        )
        _make_fenced(conn)
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["status_counts"].get("blocked") == 1
    assert diag["status_counts"].get("fenced") == 1
    assert diag["open_obligations"] == 2


def test_r12_fenced_included_in_nonterminal_detail(kanban_home):
    """The executable_zero finding lists fenced in nonterminal detail."""
    with kb.connect() as conn:
        blocked = kb.create_task(conn, title="blocked", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (blocked,)
        )
        _make_fenced(conn)
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 0
    finding = diag["findings"][0]
    assert finding["kind"] == "executable_zero_open_obligations"
    nonterminal = finding["data"]["nonterminal_states"]
    assert nonterminal.get("blocked") == 1
    assert nonterminal.get("fenced") == 1
    assert "fenced" in finding["data"]["open_statuses"]


def test_r12_done_and_archived_are_only_terminal_exclusions(kanban_home):
    """done and archived are excluded from obligations; fenced is not."""
    with kb.connect() as conn:
        done = kb.create_task(conn, title="done", assignee="a")
        kb.claim_task(conn, done)
        kb.complete_task(conn, done, result="ok")
        arch = kb.create_task(conn, title="archived", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?", (arch,)
        )
        _make_fenced(conn)
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["open_obligations"] == 1  # only the fenced task
    assert diag["status_counts"].get("done") == 1
    assert "archived" not in diag["status_counts"]  # excluded from counts


def test_r12_unknown_future_status_is_fail_closed(kanban_home):
    """An unknown nonterminal status must count as an open obligation."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="future-status", assignee="a")
        conn.execute("UPDATE tasks SET status = 'zombie' WHERE id = ?", (t,))
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["status_counts"].get("zombie") == 1
    assert diag["open_obligations"] == 1


# ── executable_now decomposition ─────────────────────────────────────────────


def test_r12_executable_now_includes_active_running(kanban_home, monkeypatch):
    """Active running work (live, identity-matched worker) is executable."""
    _live_worker(monkeypatch)
    with kb.connect() as conn:
        t = _claim_running(conn, "active-running", pid=424243)
        _spawn_worker(conn, t, pid=424243, starttime=222333)
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 1
    assert diag["executable_components"]["active_running"] == 1
    assert diag["executable_components"]["reclaimable_running"] == 0
    assert diag["executable_components"]["deferred_running"] == 0
    assert diag["executable_components"]["dispatchable_ready"] == 0
    assert diag["open_obligations"] == 1


def test_r12_executable_now_includes_reclaimable_running(kanban_home, monkeypatch):
    """A dead host-local worker PID is machine-proven reclaimable."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect() as conn:
        _claim_running(
            conn, "reclaimable-running", pid=424244,
            expires=int(time.time()) - 60,
        )
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 1
    assert diag["executable_components"]["reclaimable_running"] == 1
    assert diag["executable_components"]["active_running"] == 0
    assert diag["executable_components"]["deferred_running"] == 0


def test_r12_executable_now_excludes_fenced_and_guarded_ready(
    kanban_home, monkeypatch,
):
    """Fenced wait states and guarded ready are NOT executable."""
    pr_url = "https://github.com/totemx-AI/subsidysmart/pull/42"
    monkeypatch.setattr(
        kb, "_resolve_github_pr_state", lambda _url: "OPEN", raising=False,
    )
    with kb.connect() as conn:
        _make_fenced(conn)
        current = kb.create_task(conn, title="guarded-ready", assignee="alice")
        owner = kb.create_task(conn, title="active-owner", assignee="bob")
        kb.add_comment(conn, current, "alice", f"Continue {pr_url}")
        kb.add_comment(conn, owner, "bob", f"Working {pr_url}")
        kb.claim_task(conn, owner)
        diag = kd.compute_board_diagnostics(conn)

    # owner (fresh claim, no worker PID yet) is active running → executable_now == 1.
    assert diag["executable_now"] == 1
    comp = diag["executable_components"]
    assert comp["fenced_waiting"] == 1
    assert comp["guarded_ready"] == 1
    assert comp["dispatchable_ready"] == 0
    assert diag["guarded_ready"] == [
        {"task_id": current, "reason": "active_pr"}
    ]
    # fenced + guarded-ready + owner are all open obligations.
    assert diag["open_obligations"] == 3


def test_r12_no_double_counting_across_all_states(kanban_home, monkeypatch):
    """Each state lands in exactly one bucket; totals are exact."""
    _live_worker(monkeypatch)
    with kb.connect() as conn:
        # dispatchable ready
        kb.create_task(conn, title="ready-a", assignee="a")
        # active running (live worker, identity match, fresh claim)
        t = _claim_running(conn, "active-running", pid=424243)
        _spawn_worker(conn, t, pid=424243, starttime=222333)
        # reclaimable running (expired claim, no worker pid)
        _claim_running(
            conn, "reclaimable-running", expires=int(time.time()) - 60,
        )
        # fenced wait
        _make_fenced(conn)
        # done
        done = kb.create_task(conn, title="done", assignee="a")
        kb.claim_task(conn, done)
        kb.complete_task(conn, done, result="ok")
        # archived
        arch = kb.create_task(conn, title="archived", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?", (arch,)
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["dispatchable_ready"] == 1
    assert comp["active_running"] == 1
    assert comp["reclaimable_running"] == 1
    assert comp["deferred_running"] == 0
    assert comp["guarded_ready"] == 0
    assert comp["fenced_waiting"] == 1

    assert diag["executable_now"] == 3  # ready + active + reclaimable
    # open obligations: ready(1) + running(2) + fenced(1) = 4
    assert diag["open_obligations"] == 4
    # no double counting: components sum to executable_now
    assert (
        comp["dispatchable_ready"]
        + comp["active_running"]
        + comp["reclaimable_running"]
    ) == diag["executable_now"]


# ── fail-closed reclaim classification (audit blockers 1–3) ──────────────────


def test_r12_stale_heartbeat_expired_live_worker_is_deferred(kanban_home, monkeypatch):
    """Stale-heartbeat expired live worker is deferred, NOT reclaimable.

    ``release_stale_claims`` would terminate this worker; that termination may
    survive (→ ``reclaim_deferred`` hold), so it is not safely reclaimable now.
    """
    _live_worker(monkeypatch)
    now = int(time.time())
    with kb.connect() as conn:
        t = _claim_running(
            conn, "wedged-running", pid=424245, expires=now - 60,
        )
        _spawn_worker(conn, t, pid=424245, starttime=222333)
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (now - 7200, t),  # 2h stale > 1h max-stale threshold
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn, now=now)

    comp = diag["executable_components"]
    assert comp["reclaimable_running"] == 0
    assert comp["active_running"] == 0
    assert comp["deferred_running"] == 1
    assert diag["executable_now"] == 0
    assert diag["open_obligations"] == 1  # still an open obligation


def test_r12_pid_recycled_worker_is_deferred(kanban_home, monkeypatch):
    """A PID-recycled impostor is neither active nor reclaimable (fail closed)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    # Live PID exists but its starttime does NOT match the recorded spawn.
    monkeypatch.setattr(
        kb,
        "_read_process_identity",
        lambda pid: {"starttime": 999999, "cwd": "/other", "pgid": 1},
    )
    with kb.connect() as conn:
        t = _claim_running(conn, "recycled-running", pid=424246)
        _spawn_worker(conn, t, pid=424246, starttime=222333)
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["active_running"] == 0
    assert comp["reclaimable_running"] == 0
    assert comp["deferred_running"] == 1
    assert diag["executable_now"] == 0


def test_r12_missing_identity_worker_is_deferred(kanban_home, monkeypatch):
    """A live PID with no recorded starttime identity is fail-closed deferred."""
    _live_worker(monkeypatch)
    with kb.connect() as conn:
        # worker_pid set, but NO spawned event and NULL worker_starttime.
        _claim_running(conn, "no-identity-running", pid=424247)
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["active_running"] == 0
    assert comp["reclaimable_running"] == 0
    assert comp["deferred_running"] == 1
    assert diag["executable_now"] == 0


def test_r12_null_current_run_id_is_deferred(kanban_home):
    """A running task with NULL current_run_id has no ownership → deferred."""
    with kb.connect() as conn:
        t = _claim_running(conn, "no-ownership-running", pid=424248)
        conn.execute(
            "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (t,),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["active_running"] == 0
    assert comp["reclaimable_running"] == 0
    assert comp["deferred_running"] == 1
    assert diag["executable_now"] == 0


def test_r12_stale_ownership_ended_run_is_deferred(kanban_home):
    """current_run_id pointing at an already-ended run row → deferred."""
    with kb.connect() as conn:
        t = _claim_running(conn, "stale-ownership-running", pid=424249)
        # Close the run row out from under the task (simulated invariant leak).
        conn.execute(
            "UPDATE task_runs SET ended_at = ?, status = 'done' "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (int(time.time()), t),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["active_running"] == 0
    assert comp["reclaimable_running"] == 0
    assert comp["deferred_running"] == 1


def test_r12_ttl_boundary_not_yet_expired_is_active(kanban_home, monkeypatch):
    """A claim expiring exactly now (not strictly before) is still active."""
    _live_worker(monkeypatch)
    now = int(time.time())
    with kb.connect() as conn:
        t = _claim_running(conn, "boundary-running", pid=424250, expires=now)
        _spawn_worker(conn, t, pid=424250, starttime=222333)
        conn.commit()
        diag = kd.compute_board_diagnostics(conn, now=now)

    comp = diag["executable_components"]
    assert comp["active_running"] == 1
    assert comp["reclaimable_running"] == 0
    assert comp["deferred_running"] == 0
    assert diag["executable_now"] == 1


def test_r12_nonhost_local_expired_claim_is_reclaimable(kanban_home):
    """A foreign (non-host-local) expired claim is safely reclaimable."""
    with kb.connect() as conn:
        t = _claim_running(
            conn, "foreign-running", pid=424251, expires=int(time.time()) - 60,
        )
        conn.execute(
            "UPDATE tasks SET claim_lock = ? WHERE id = ?",
            ("otherhost:999", t),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["reclaimable_running"] == 1
    assert comp["active_running"] == 0
    assert comp["deferred_running"] == 0
    assert diag["executable_now"] == 1


def test_r12_host_local_no_pid_expired_is_reclaimable(kanban_home):
    """A host-local claim with no worker PID, expired, is safely reclaimable."""
    with kb.connect() as conn:
        _claim_running(
            conn, "pidless-expired", expires=int(time.time()) - 60,
        )
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["reclaimable_running"] == 1
    assert comp["active_running"] == 0
    assert comp["deferred_running"] == 0
    assert diag["executable_now"] == 1


# ── detached / stale run rows must never count as active work ────────────────


def test_r12_stale_run_row_never_counts_as_active_work(kanban_home, monkeypatch):
    """A done task with an orphaned running run row is NOT active work."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="product", assignee="a")
        kb.claim_task(conn, t)                # run 1 (running)
        kb.complete_task(conn, t, result="done")  # run 1 → completed, task done
        # Inject an orphaned stale run row (running / ended_at NULL) that is
        # NOT the task's current_run_id — the historical leak from the live
        # board (t_8e8e8d62 row 664).
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "worker_pid, claim_lock) VALUES (?, ?, 'running', ?, ?, ?)",
            (t, "a", int(time.time()) - 3600, 999999, "host:dead"),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 0
    assert diag["open_obligations"] == 0
    assert diag["executable_components"]["active_running"] == 0
    assert diag["executable_components"]["reclaimable_running"] == 0
    assert diag["executable_components"]["deferred_running"] == 0


# ── hermetic environment-precedence isolation ────────────────────────────────


def test_r12_kanban_db_env_precedence(tmp_path, monkeypatch):
    """HERMES_KANBAN_DB outranks HERMES_HOME; connect() honors the pin.

    Regression for the audit's isolation correction: a probe that inherited
    ``HERMES_KANBAN_DB`` while only monkeypatching ``HERMES_HOME`` wrote fake
    rows into the live board. The pin must win, so a hermetic test binds it to
    the temp root rather than relying on it being absent.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    pinned = tmp_path / "pinned-kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert str(kb.kanban_db_path()) == str(pinned)

    with kb.connect() as conn:
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(db_file).resolve() == pinned.resolve()
    assert not (home / "kanban.db").exists()


def test_r12_isolated_fixture_never_touches_default_home(kanban_home, tmp_path):
    """The isolated fixture routes connect() to the temp DB, not <home>/kanban.db."""
    with kb.connect() as conn:
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
        t = kb.create_task(conn, title="hermetic-probe", assignee="a")
        conn.commit()

    # Writes landed inside the temp fixture DB, never a HERMES_HOME default.
    assert Path(db_file).resolve().parent == (tmp_path / ".hermes").resolve()
    # And the task actually persisted there (readable on a fresh connection).
    with kb.connect() as conn:
        assert kb.get_task(conn, t) is not None
