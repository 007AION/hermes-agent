"""Regression tests for AION-RL2-CORE-01-R10: a budget-exhausted worker must be
fenced before the task becomes re-claimable.

Canonical incident t_e690dcc1 / t_50c0b14c: run 1931 recorded ``timed_out`` and
released its claim while predecessor PID 559640/starttime 192058466 was still
alive; run 1932 then claimed/spawned into the same scratch workspace 28s later.
The invariant under repair: a task must NOT be dispatchable while the exact
predecessor worker PID+starttime can still mutate its workspace.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim_with_worker(conn, assignee="a", pid=12345):
    t = kb.create_task(conn, title="budget", assignee=assignee)
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, t, claimer=f"{host}:worker")
    kb._set_worker_pid(conn, t, pid)
    return t


def _make_fenced(conn, pid, starttime, *, workspace_kind="scratch", workspace_path=None):
    t = _claim_with_worker(conn, pid=pid)
    conn.execute(
        "UPDATE tasks SET status='fenced', worker_starttime=?, claim_lock=NULL, "
        "claim_expires=NULL, workspace_kind=?, workspace_path=? WHERE id = ?",
        (starttime, workspace_kind, workspace_path, t),
    )
    conn.commit()
    return t


# ---------------------------------------------------------------------------
# Recording path (fence_worker=True)
# ---------------------------------------------------------------------------

def test_fence_worker_records_fenced_not_claimable_while_predecessor_alive(
    kanban_home, monkeypatch,
):
    """Recording a self-timeout must NOT make the task claimable while the exact
    predecessor PID is still alive."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        _kb, "_read_process_identity",
        lambda _pid: {"starttime": 111222, "cwd": "/w", "pgid": 1},
    )
    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=424242)

        _kb._record_task_failure(
            conn, t,
            error="Iteration budget exhausted (60/60)",
            outcome="timed_out",
            fence_worker=True,
            end_run=True,
        )

        task = kb.get_task(conn, t)
        assert task.status == "fenced", task.status
        row = conn.execute(
            "SELECT worker_pid, worker_starttime, claim_lock FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["worker_pid"] == 424242       # predecessor identity retained
        assert row["worker_starttime"] == 111222  # starttime captured for fencing
        assert row["claim_lock"] is None

        # A second worker must NOT be claimable now.
        assert kb.claim_task(conn, t) is None

        # Failure counter / circuit-breaker and run outcome stay correct.
        assert conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?", (t,),
        ).fetchone()[0] == 1
        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert run["outcome"] == "timed_out"

        # Machine-readable fence evidence on the existing event surface.
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'fenced'",
            (t,),
        ).fetchone()
        assert ev is not None
        payload = json.loads(ev["payload"])
        assert payload["pid"] == 424242
        assert payload["starttime"] == 111222


def test_fence_worker_trips_breaker_blocks_and_clears_identity(
    kanban_home, monkeypatch,
):
    """When the breaker trips on a self-fence, the task is blocked (non-dispatchable)
    and the stale predecessor identity is dropped."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_read_process_identity", lambda _pid: None)
    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=424243)

        blocked = _kb._record_task_failure(
            conn, t,
            error="Iteration budget exhausted",
            outcome="timed_out",
            fence_worker=True,
            end_run=True,
            failure_limit=1,  # trip on first failure
        )
        assert blocked is True

        task = kb.get_task(conn, t)
        assert task.status == "blocked"
        row = conn.execute(
            "SELECT worker_pid, worker_starttime FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["worker_pid"] is None
        assert row["worker_starttime"] is None


def test_fence_worker_with_no_predecessor_pid_degrades_to_ready(
    kanban_home, monkeypatch,
):
    """If worker_pid was never recorded there is no predecessor to fence — release."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        # worker_pid left NULL.

        _kb._record_task_failure(
            conn, t, error="boom", outcome="timed_out",
            fence_worker=True, end_run=True,
        )
        assert kb.get_task(conn, t).status == "ready"


# ---------------------------------------------------------------------------
# Reconciliation path (release_fenced_workers)
# ---------------------------------------------------------------------------

def test_release_fenced_workers_releases_only_after_verified_exit(
    kanban_home, monkeypatch,
):
    """After the predecessor PID is verified dead, the task becomes retry-eligible."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424244, starttime=999000)
        monkeypatch.setattr(_kb, "_read_process_identity", lambda _pid: None)
        released = kb.release_fenced_workers(conn)
        assert t in released
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        row = conn.execute(
            "SELECT worker_pid, worker_starttime FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["worker_pid"] is None
        assert row["worker_starttime"] is None
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (t,),
        ).fetchall()]
        assert "fence_released" in kinds


def test_release_fenced_workers_releases_on_pid_recycled_without_signal(
    kanban_home, monkeypatch,
):
    """A starttime mismatch means the PID was recycled — predecessor gone. The
    recycled occupant must never be signalled, and the task is released."""
    import hermes_cli.kanban_db as _kb

    fence_calls = []
    monkeypatch.setattr(
        _kb, "_read_process_identity",
        lambda _pid: {"starttime": 999999, "cwd": "/elsewhere", "pgid": 1},
    )
    monkeypatch.setattr(
        _kb, "close_workspace_processes",
        lambda *a, **k: fence_calls.append((a, k)),
    )
    monkeypatch.setattr(_kb, "_fence_worker_by_identity",
                        lambda *a, **k: fence_calls.append(("direct", a, k)))

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424245, starttime=999000)
        released = kb.release_fenced_workers(conn)
        assert t in released
        assert fence_calls == []  # never signalled the recycled PID
        assert kb.get_task(conn, t).status == "ready"


def test_release_fenced_workers_unknown_identity_fails_closed(
    kanban_home, monkeypatch,
):
    """No starttime identity proof + predecessor alive → never signal; stay fenced."""
    import hermes_cli.kanban_db as _kb

    fence_calls = []
    monkeypatch.setattr(_kb, "_read_process_identity",
                        lambda _pid: {"starttime": 555, "cwd": "/w", "pgid": 1})
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(_kb, "close_workspace_processes",
                        lambda *a, **k: fence_calls.append(1))

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424246, starttime=None)
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert fence_calls == []
        assert kb.get_task(conn, t).status == "fenced"
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (t,),
        ).fetchall()]
        assert "fence_deferred" in kinds


def test_release_fenced_workers_fences_live_predecessor_then_releases(
    kanban_home, monkeypatch, tmp_path,
):
    """Alive + identity match → fence via close_workspace_processes, then release."""
    import hermes_cli.kanban_db as _kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    state = {"fenced": False}
    fence_calls = []

    def _ident(pid):
        if state["fenced"]:
            return None  # gone after the fence
        return {"starttime": 777, "cwd": str(workspace), "pgid": 1}

    def _close(workspace_arg, **kwargs):
        fence_calls.append((workspace_arg, kwargs))
        state["fenced"] = True  # simulate the worker dying inside the fence

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "close_workspace_processes", _close)

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424247, starttime=777,
                         workspace_path=str(workspace))
        released = kb.release_fenced_workers(conn)
        assert t in released
        assert len(fence_calls) == 1  # close_workspace_processes invoked
        assert kb.get_task(conn, t).status == "ready"


def test_release_fenced_workers_surviving_predecessor_stays_fenced(
    kanban_home, monkeypatch, tmp_path,
):
    """If the predecessor survives the fence, the task stays fenced (no duplicate)."""
    import hermes_cli.kanban_db as _kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    def _ident(pid):
        return {"starttime": 778, "cwd": str(workspace), "pgid": 1}

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "close_workspace_processes", lambda *a, **k: None)

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424248, starttime=778,
                         workspace_path=str(workspace))
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert kb.claim_task(conn, t) is None  # still not claimable


def test_release_fenced_workers_shared_dir_gates_on_owned_pids(
    kanban_home, monkeypatch, tmp_path,
):
    """For a shared-dir workspace, the fence must pass the worker's owned PID
    lineage so unrelated processes sharing the directory are never signalled."""
    import hermes_cli.kanban_db as _kb

    shared = tmp_path / "shared"
    shared.mkdir()

    owned_seen = {}

    def _ident(pid):
        return {"starttime": 880, "cwd": str(shared), "pgid": 1}

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "_discover_descendant_pids",
                        lambda pid, **k: {pid, pid + 1})
    monkeypatch.setattr(
        _kb, "close_workspace_processes",
        lambda workspace, **k: owned_seen.update(k),
    )

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424249, starttime=880,
                         workspace_kind="dir", workspace_path=str(shared))
        kb.release_fenced_workers(conn)
    # owned_pids passed (worker + descendant), never None for a shared dir.
    assert "owned_pids" in owned_seen
    assert owned_seen["owned_pids"] == {424249, 424250}


# ---------------------------------------------------------------------------
# Identity-safe fence helper
# ---------------------------------------------------------------------------

def test_fence_worker_by_identity_never_signals_self(kanban_home):
    import hermes_cli.kanban_db as _kb

    signalled = []
    result = _kb._fence_worker_by_identity(
        os.getpid(), 123, signal_fn=lambda p, s: signalled.append((p, s)),
    )
    assert signalled == []
    assert result["termination_attempted"] is False


def test_fence_worker_by_identity_pid_reuse_withholds_signal(kanban_home, monkeypatch):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(
        _kb, "_read_process_identity",
        lambda _pid: {"starttime": 424242, "cwd": "/w", "pgid": 1},
    )
    signalled = []
    result = _kb._fence_worker_by_identity(
        999999, 111111,  # recorded starttime != current → recycled
        signal_fn=lambda p, s: signalled.append((p, s)),
    )
    assert signalled == []
    assert result["identity_mismatch"] is True


def test_fence_worker_by_identity_signals_matching_identity(kanban_home, monkeypatch):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(
        _kb, "_read_process_identity",
        lambda _pid: {"starttime": 424242, "cwd": "/w", "pgid": 1},
    )
    # Simulate immediate exit after SIGTERM (identity gone).
    monkeypatch.setattr(_kb, "_revalidate_identity", lambda _pid, _cap: False)

    signalled = []
    result = _kb._fence_worker_by_identity(
        999999, 424242, signal_fn=lambda p, s: signalled.append((p, s)),
    )
    assert signalled == [(999999, signal.SIGTERM)]
    assert result["terminated"] is True


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------

def test_dispatch_reconciles_fenced_before_spawning(
    kanban_home, monkeypatch, all_assignees_spawnable,
):
    """A fenced task with a verified-dead predecessor is released and re-spawned
    by the dispatcher in the same tick; a fenced task with a live predecessor is
    left alone (not spawned)."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        dead = _make_fenced(conn, pid=500001, starttime=1000)
        live = _make_fenced(conn, pid=500002, starttime=1000)
        # dead predecessor: identity unreadable; live predecessor: identity present.
        def _ident(pid):
            if pid == 500001:
                return None
            return {"starttime": 1000, "cwd": "/w", "pgid": 1}
        monkeypatch.setattr(_kb, "_read_process_identity", _ident)
        monkeypatch.setattr(_kb, "close_workspace_processes", lambda *a, **k: None)
        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: True)

        spawns = []
        def _spawn(task, workspace):
            spawns.append(task.id)

        res = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert dead in res.fenced_released
        # Only the released (dead-predecessor) task is spawnable.
        assert dead in spawns
        assert live not in spawns
        # The live-predecessor task remains fenced, not claimable.
        assert kb.get_task(conn, live).status == "fenced"
