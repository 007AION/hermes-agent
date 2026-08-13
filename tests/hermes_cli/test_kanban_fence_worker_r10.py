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


def test_fence_worker_trips_breaker_fences_and_retains_identity(
    kanban_home, monkeypatch,
):
    """When the breaker trips on a self-fence, the task is parked in ``fenced``
    (non-dispatchable) with the live predecessor identity RETAINED — never
    blocked-with-dropped-identity, which would let a still-alive worker keep
    mutating the workspace and a later unblock spawn a duplicate beside it.

    (AION-RL2-CORE-01-R10 PR#8 repair — canonical t_e690dcc1 / t_6918bd42 runs
    1964/1984: the breaker branch dropped worker_pid/starttime while the exact
    predecessor PID was still alive.)"""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(
        _kb, "_read_process_identity",
        lambda _pid: {"starttime": 111333, "cwd": "/w", "pgid": 1},
    )
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
        assert task.status == "fenced"          # non-dispatchable, not blocked
        row = conn.execute(
            "SELECT worker_pid, worker_starttime, claim_lock FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["worker_pid"] == 424243       # predecessor identity retained
        assert row["worker_starttime"] == 111333  # starttime captured for fencing
        assert row["claim_lock"] is None

        # A second worker must NOT be claimable while the predecessor lives.
        assert kb.claim_task(conn, t) is None



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


def test_release_fenced_workers_descendant_survivor_keeps_fenced(
    kanban_home, monkeypatch, tmp_path,
):
    """Root PID exits during the fence but an eligible in-workspace descendant
    survives (``close_workspace_processes`` reports ``survivors > 0``) — the
    task must stay ``fenced`` and non-claimable until the complete lineage is
    gone, never released on root-PID disappearance alone.

    Regression for the bafuxunan exact-head audit finding: the R10 code
    discarded ``close_workspace_processes().survivors`` and released to ready
    as soon as the root PID disappeared, redispatching into a workspace that a
    surviving descendant could still mutate.
    """
    import hermes_cli.kanban_db as _kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    state = {"root_fenced": False}

    def _ident(pid):
        if state["root_fenced"]:
            return None  # root exited during the fence
        return {"starttime": 999001, "cwd": str(workspace), "pgid": 1}

    def _close(workspace_arg, **kwargs):
        state["root_fenced"] = True  # root died; descendant below survived
        return {
            "workspace": str(workspace),
            "signalled": 2,
            "terminated": 1,
            "killed": 0,
            "survivors": 1,  # an eligible descendant is still alive
        }

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "close_workspace_processes", _close)

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424250, starttime=999001,
                         workspace_path=str(workspace))
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert kb.claim_task(conn, t) is None  # still not claimable
        # Deferred specifically for the surviving descendant, not a root-only
        # reason, so operators can see the fence was held for lineage reasons.
        deferred = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'fence_deferred' ORDER BY created_at DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert deferred is not None
        assert json.loads(deferred["payload"])["reason"] == "descendant_survived"


def test_release_fenced_workers_descendant_clean_exit_releases(
    kanban_home, monkeypatch, tmp_path,
):
    """When the fence reports zero survivors AND the root PID is gone, the task
    is released — the complete eligible lineage is quiescent."""
    import hermes_cli.kanban_db as _kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    state = {"root_fenced": False}

    def _ident(pid):
        if state["root_fenced"]:
            return None
        return {"starttime": 999002, "cwd": str(workspace), "pgid": 1}

    def _close(workspace_arg, **kwargs):
        state["root_fenced"] = True
        return {
            "workspace": str(workspace),
            "signalled": 2,
            "terminated": 2,
            "killed": 0,
            "survivors": 0,
        }

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "close_workspace_processes", _close)

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=424251, starttime=999002,
                         workspace_path=str(workspace))
        released = kb.release_fenced_workers(conn)
        assert t in released
        assert kb.get_task(conn, t).status == "ready"


def test_release_fenced_workers_multitick_descendant_survivor_not_released_on_root_exit(
    kanban_home, monkeypatch, tmp_path,
):
    """Multi-tick regression: after tick 1 records ``descendant_survived`` and
    the root PID exits, tick 2 must NOT release solely on root disappearance —
    the persisted eligible descendant must be rechecked and, while still alive,
    keep the task fenced/non-claimable. Only when the descendant also exits does
    the task release through final zero-survivor.

    Regression for the bafuxunan exact-head audit (t_c09288bc) finding: the R10
    repair handled the single-tick survivor but released on the next tick's
    missing-root identity without rediscovering/persisting the still-live
    descendant.
    """
    import hermes_cli.kanban_db as _kb

    shared = tmp_path / "shared"
    shared.mkdir()

    root_pid = 424260
    root_starttime = 999020
    desc_pid = 900020
    desc_starttime = 999021

    state = {"root_alive": True, "desc_alive": True, "cleanup_ran": 0}

    def _ident(pid):
        if pid == root_pid:
            if state["root_alive"]:
                return {"starttime": root_starttime, "cwd": str(shared), "pgid": 1}
            return None
        if pid == desc_pid:
            if state["desc_alive"]:
                return {"starttime": desc_starttime, "cwd": str(shared), "pgid": 1}
            return None
        return None

    def _close(workspace_arg, **kwargs):
        state["cleanup_ran"] += 1
        # Root dies inside the fence; the owned descendant survives.
        state["root_alive"] = False
        return {
            "workspace": str(shared),
            "signalled": 2,
            "terminated": 1,
            "killed": 0,
            "survivors": 1,
            "pids": [(1, desc_pid, str(shared), "survivor")],
        }

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(
        _kb, "_discover_descendant_pids",
        lambda pid, **k: {root_pid, desc_pid},
    )
    monkeypatch.setattr(_kb, "close_workspace_processes", _close)

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=root_pid, starttime=root_starttime,
                         workspace_kind="dir", workspace_path=str(shared))

        # Tick 1: root alive + identity match → fence discovers descendant survivor.
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert state["cleanup_ran"] == 1
        assert state["root_alive"] is False  # root died inside the fence

        # Tick 2: root PID gone, but the persisted descendant is still alive —
        # must NOT release.
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert kb.claim_task(conn, t) is None  # non-claimable

        # Tick 3: descendant finally exits → final zero-survivor release.
        state["desc_alive"] = False
        released = kb.release_fenced_workers(conn)
        assert t in released
        assert kb.get_task(conn, t).status == "ready"


def test_release_fenced_workers_root_gone_before_first_tick_orphan_keeps_fenced(
    kanban_home, monkeypatch, tmp_path,
):
    """If the root PID is already gone on the first reconciler tick, an orphaned
    descendant still bound to an exclusive (scratch) workspace must be
    rediscovered by cwd containment and keep the task fenced until it exits.
    """
    import hermes_cli.kanban_db as _kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    root_pid = 424261
    root_starttime = 999030
    orphan_pid = 900030
    orphan_starttime = 999031

    state = {"orphan_alive": True}

    def _ident(pid):
        if pid == root_pid:
            return None  # root already exited
        if pid == orphan_pid:
            if state["orphan_alive"]:
                return {"starttime": orphan_starttime, "cwd": str(workspace), "pgid": 1}
            return None
        return None

    def _close(workspace_arg, **kwargs):
        if state["orphan_alive"]:
            return {
                "workspace": str(workspace),
                "signalled": 1,
                "terminated": 0,
                "killed": 0,
                "survivors": 1,
                "pids": [(1, orphan_pid, str(workspace), "survivor")],
            }
        return {
            "workspace": str(workspace),
            "signalled": 0,
            "terminated": 0,
            "killed": 0,
            "survivors": 0,
            "pids": [],
        }

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "close_workspace_processes", _close)

    with kb.connect() as conn:
        t = _make_fenced(conn, pid=root_pid, starttime=root_starttime,
                         workspace_kind="scratch", workspace_path=str(workspace))

        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"

        state["orphan_alive"] = False
        released = kb.release_fenced_workers(conn)
        assert t in released
        assert kb.get_task(conn, t).status == "ready"


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
# Breaker-trip terminal disposition (AION-RL2-CORE-01-R10 PR#8 repair)
# ---------------------------------------------------------------------------

def test_fence_worker_breaker_trip_no_predecessor_blocks_terminal(kanban_home):
    """No predecessor identity + breaker trip → terminal ``blocked`` (gave_up),
    never a retry-eligible ``ready``."""
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        # worker_pid left NULL.

        blocked = _kb._record_task_failure(
            conn, t, error="boom", outcome="timed_out",
            fence_worker=True, end_run=True, failure_limit=1,
        )
        assert blocked is True
        assert kb.get_task(conn, t).status == "blocked"


def test_fence_worker_breaker_trip_terminalizes_blocked_after_quiescence(
    kanban_home, monkeypatch, tmp_path,
):
    """After the breaker-tripped predecessor lineage is quiescent, the task
    terminalizes to ``blocked`` (gave_up) — NOT ``ready`` — so the dispatcher
    does NOT retry the work. Deterministic terminal disposition without retry."""
    import hermes_cli.kanban_db as _kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    monkeypatch.setattr(
        _kb, "_read_process_identity",
        lambda _pid: {"starttime": 777001, "cwd": str(workspace), "pgid": 1},
    )
    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=424301)
        conn.execute(
            "UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id = ?",
            (str(workspace), t),
        )
        conn.commit()
        _kb._record_task_failure(
            conn, t, error="boom", outcome="timed_out",
            fence_worker=True, end_run=True, failure_limit=1,
        )
        assert kb.get_task(conn, t).status == "fenced"

    # Predecessor gone + zero survivors → deterministic terminalization.
    monkeypatch.setattr(_kb, "_read_process_identity", lambda _pid: None)
    monkeypatch.setattr(
        _kb, "close_workspace_processes",
        lambda *a, **k: {"workspace": str(workspace), "survivors": 0, "pids": []},
    )
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert t not in released          # not released back to ready
        assert kb.get_task(conn, t).status == "blocked"   # terminal, no retry
        assert kb.claim_task(conn, t) is None
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'fence_released' ORDER BY created_at DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert ev is not None
        assert json.loads(ev["payload"]).get("target") == "blocked"


def test_fence_worker_breaker_trip_pid_recycled_terminalizes_blocked_without_signal(
    kanban_home, monkeypatch,
):
    """A recycled (starttime-mismatch) predecessor must never be signalled, and a
    breaker-tripped fenced task still terminalizes to ``blocked`` (fail closed)."""
    import hermes_cli.kanban_db as _kb

    state = {"phase": "record"}
    fence_calls = []

    def _ident(pid):
        if state["phase"] == "record":
            return {"starttime": 111, "cwd": "/w", "pgid": 1}
        return {"starttime": 999999, "cwd": "/elsewhere", "pgid": 1}

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "_fence_worker_by_identity",
                        lambda *a, **k: fence_calls.append(("direct", a, k)))
    monkeypatch.setattr(_kb, "close_workspace_processes",
                        lambda *a, **k: fence_calls.append(("close", a, k)))

    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=424320)
        _kb._record_task_failure(
            conn, t, error="boom", outcome="timed_out",
            fence_worker=True, end_run=True, failure_limit=1,
        )
        assert kb.get_task(conn, t).status == "fenced"

    state["phase"] = "release"
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert t not in released
        assert kb.get_task(conn, t).status == "blocked"
        assert fence_calls == []  # never signalled the recycled PID


def test_fence_worker_breaker_trip_descendant_survivor_keeps_fenced_then_blocked(
    kanban_home, monkeypatch, tmp_path,
):
    """Multi-tick: a breaker-tripped fenced task whose root exits while an owned
    descendant survives must stay ``fenced`` (non-dispatchable, not released,
    not terminalized) across >=2 dispatcher ticks; only the final zero-survivor
    terminalizes to ``blocked`` without retrying work."""
    import hermes_cli.kanban_db as _kb

    shared = tmp_path / "shared"
    shared.mkdir()

    root_pid = 424310
    root_starttime = 777310
    desc_pid = 900310
    desc_starttime = 777311

    state = {"root_alive": True, "desc_alive": True, "owned_seen": None}

    def _ident(pid):
        if pid == root_pid:
            if state["root_alive"]:
                return {"starttime": root_starttime, "cwd": str(shared), "pgid": 1}
            return None
        if pid == desc_pid:
            if state["desc_alive"]:
                return {"starttime": desc_starttime, "cwd": str(shared), "pgid": 1}
            return None
        return None

    def _close(workspace_arg, **kwargs):
        state["owned_seen"] = kwargs.get("owned_pids")
        state["root_alive"] = False
        return {
            "workspace": str(shared),
            "signalled": 2, "terminated": 1, "killed": 0,
            "survivors": 1,
            "pids": [(1, desc_pid, str(shared), "survivor")],
        }

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(_kb, "_discover_descendant_pids",
                        lambda pid, **k: {root_pid, desc_pid})
    monkeypatch.setattr(_kb, "close_workspace_processes", _close)

    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=root_pid)
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id = ?",
            (str(shared), t),
        )
        conn.commit()
        _kb._record_task_failure(
            conn, t, error="boom", outcome="timed_out",
            fence_worker=True, end_run=True, failure_limit=1,
        )
        assert kb.get_task(conn, t).status == "fenced"

    # Tick 1: root alive + identity match → fence discovers descendant survivor.
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert state["root_alive"] is False
        # Shared-dir fence is gated on the worker's owned PID lineage.
        assert state["owned_seen"] is not None
        assert state["owned_seen"] == {root_pid, desc_pid}

    # Tick 2: root gone, persisted descendant alive → still fenced (not released,
    # not terminalized) — persists across a second dispatcher tick.
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert kb.claim_task(conn, t) is None

    # Tick 3: descendant finally exits → final zero-survivor → terminal blocked.
    state["desc_alive"] = False
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert t not in released
        assert kb.get_task(conn, t).status == "blocked"


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


# ---------------------------------------------------------------------------
# Hermetic probe isolation (Native Kanban env pins)
# ---------------------------------------------------------------------------

def test_isolated_kanban_env_keeps_inherited_live_board_untouched(
    kanban_home, monkeypatch, tmp_path,
):
    """A standalone RED/audit probe that inherits the dispatcher's live
    ``HERMES_KANBAN_DB`` / board / workspaces pins must not write synthetic
    cards into the real board.

    Regression for the AION-RL2-CORE-01-R10 pollution incident: two standalone
    probes inherited the dispatched worker's live ``HERMES_KANBAN_DB`` and
    wrote synthetic fixture residue (t_d4dffd9c / t_75d7b60e) into the live
    aion-factory board. ``isolated_kanban_env`` must rebind every pin so
    ``connect()`` resolves to an isolated temp DB, leaving the live board
    byte-identical.
    """
    import hermes_cli.kanban_db as _kb

    # Simulate the live board a dispatched worker's env points at.
    live_home = tmp_path / "live-factory"
    live_home.mkdir()
    live_db = live_home / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "aion-factory")
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(live_home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(live_home / "workspaces"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(live_home / "attachments"))

    # Seed the live board with a known task so any residue is detectable.
    with _kb.connect() as live_conn:
        seed = _kb.create_task(live_conn, title="real-factory-task", assignee="a")
        live_ids_before = [
            r["id"] for r in live_conn.execute(
                "SELECT id FROM tasks ORDER BY id"
            ).fetchall()
        ]

    iso = tmp_path / "isolated-probe"
    iso.mkdir()
    with _kb.isolated_kanban_env(iso):
        with _kb.connect() as probe_conn:
            created = _kb.create_task(probe_conn, title="probe fixture")

    # The live board is untouched (no synthetic residue); the probe task
    # landed in the isolated DB under the temp root, not the live board.
    with _kb.connect() as live_conn:
        live_ids_after = [
            r["id"] for r in live_conn.execute(
                "SELECT id FROM tasks ORDER BY id"
            ).fetchall()
        ]
    assert live_ids_after == live_ids_before
    assert seed in live_ids_after
    assert created not in live_ids_after

    # After the context manager exits, the inherited live pins are restored.
    assert os.environ.get("HERMES_KANBAN_DB") == str(live_db)
    assert os.environ.get("HERMES_KANBAN_BOARD") == "aion-factory"


# ---------------------------------------------------------------------------
# AION-RL2-CORE-01-R11: persist reparented outside-cwd descendant lineage at
# self-fence time. Canonical run 2041: the reconciler released a breaker-tripped
# task to ``blocked`` with reason=predecessor_exited while an exact task-owned
# descendant (PID+starttime) was still alive in the same cgroup with cwd=/tmp.
# The descendant lineage was never captured while ancestry was provable (the
# worker had already exited before the first reconciler tick), so the task was
# falsely released with no fence_deferred(descendant_survived) event and no
# persisted fence_lineage.
# ---------------------------------------------------------------------------

def _assert_fence_lineage(conn, task_id, expected):
    raw = conn.execute(
        "SELECT fence_lineage FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()[0]
    assert raw is not None
    assert json.loads(raw) == expected


def _record_breaker_self_fence(conn, t, shared):
    import hermes_cli.kanban_db as _kb
    conn.execute(
        "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id = ?",
        (str(shared), t),
    )
    conn.commit()
    blocked = _kb._record_task_failure(
        conn, t, error="boom", outcome="timed_out",
        fence_worker=True, end_run=True, failure_limit=1,
    )
    assert blocked is True
    assert kb.get_task(conn, t).status == "fenced"
    return conn


def test_fence_worker_breaker_trip_persists_reparented_outside_cwd_descendant_lineage(
    kanban_home, monkeypatch, tmp_path,
):
    """run-2041 RED/GREEN (breaker disposition=blocked): a breaker-tripped
    self-fence must capture the worker's exact descendant lineage (PID+starttime)
    BEFORE the worker exits, persist it as ``fence_lineage``, and keep the task
    fenced/non-claimable until that lineage — including a descendant that escaped
    the workspace cwd and was reparented — actually exits. Terminal ``blocked``
    only after lineage quiescence, never on root disappearance alone."""
    import hermes_cli.kanban_db as _kb

    shared = tmp_path / "shared"
    shared.mkdir()

    root_pid = 424400
    root_starttime = 777400
    desc_pid = 900400
    desc_starttime = 777401

    state = {"root_alive": True, "desc_alive": True}
    close_calls = []

    def _ident(pid):
        if pid == root_pid:
            if state["root_alive"]:
                return {"starttime": root_starttime, "cwd": str(shared), "pgid": 1}
            return None
        if pid == desc_pid:
            if state["desc_alive"]:
                # Reparented + escaped cwd — the exact run-2041 shape.
                return {"starttime": desc_starttime, "cwd": "/tmp", "pgid": 2}
            return None
        return None

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(
        _kb, "_discover_descendant_pids",
        lambda pid, **k: {root_pid, desc_pid},
    )
    # The escaped descendant is WAIT-ONLY lineage: it must never authorize a
    # workspace signal. For a shared-dir task with the root already gone, the
    # reconciler only polls the persisted lineage — close_workspace_processes
    # is never invoked.
    monkeypatch.setattr(
        _kb, "close_workspace_processes",
        lambda *a, **k: close_calls.append((a, k)) or {"survivors": 0, "pids": []},
    )

    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=root_pid)
        _record_breaker_self_fence(conn, t, shared)

        # The descendant lineage is persisted at self-fence time, while the
        # worker was still alive and ancestry was provable.
        _assert_fence_lineage(
            conn, t, [{"pid": desc_pid, "starttime": desc_starttime}],
        )

    # Root exits between the self-fence and the first reconciler tick.
    state["root_alive"] = False

    # Tick 1: root gone + reparented descendant alive → NOT released, NOT
    # terminalized — held for lineage with reason=descendant_survived.
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert kb.claim_task(conn, t) is None
        deferred = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'fence_deferred' ORDER BY created_at DESC LIMIT 1",
            (t,),
        ).fetchone()
        assert deferred is not None
        assert json.loads(deferred["payload"])["reason"] == "descendant_survived"

    # Tick 2: descendant still alive → still fenced (multi-tick nonclaimability).
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"

    # Tick 3: descendant finally exits → terminal blocked (no retry).
    state["desc_alive"] = False
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert t not in released
        assert kb.get_task(conn, t).status == "blocked"

    # The escaped descendant was never signalled across any tick.
    assert close_calls == []


def test_fence_worker_below_threshold_persists_reparented_outside_cwd_descendant_lineage(
    kanban_home, monkeypatch, tmp_path,
):
    """retry disposition=ready variant: a below-threshold self-fence must persist
    the escaped-descendant lineage and release to ``ready`` only after lineage
    quiescence, across multiple reconciler ticks."""
    import hermes_cli.kanban_db as _kb

    shared = tmp_path / "shared"
    shared.mkdir()

    root_pid = 424410
    root_starttime = 777410
    desc_pid = 900410
    desc_starttime = 777411

    state = {"root_alive": True, "desc_alive": True}
    close_calls = []

    def _ident(pid):
        if pid == root_pid:
            if state["root_alive"]:
                return {"starttime": root_starttime, "cwd": str(shared), "pgid": 1}
            return None
        if pid == desc_pid:
            if state["desc_alive"]:
                return {"starttime": desc_starttime, "cwd": "/tmp", "pgid": 2}
            return None
        return None

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(
        _kb, "_discover_descendant_pids",
        lambda pid, **k: {root_pid, desc_pid},
    )
    monkeypatch.setattr(
        _kb, "close_workspace_processes",
        lambda *a, **k: close_calls.append((a, k)) or {"survivors": 0, "pids": []},
    )

    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=root_pid)
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id = ?",
            (str(shared), t),
        )
        conn.commit()
        blocked = _kb._record_task_failure(
            conn, t, error="boom", outcome="timed_out",
            fence_worker=True, end_run=True,
            # No failure_limit → below DEFAULT threshold → retry disposition.
        )
        assert blocked is False
        assert kb.get_task(conn, t).status == "fenced"
        _assert_fence_lineage(
            conn, t, [{"pid": desc_pid, "starttime": desc_starttime}],
        )

    state["root_alive"] = False

    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"
        assert kb.claim_task(conn, t) is None

    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert released == []
        assert kb.get_task(conn, t).status == "fenced"

    state["desc_alive"] = False
    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert t in released
        assert kb.get_task(conn, t).status == "ready"

    assert close_calls == []


def test_fence_worker_reparented_descendant_pid_reuse_releases(
    kanban_home, monkeypatch, tmp_path,
):
    """Negative: a captured descendant whose PID was recycled (starttime mismatch)
    is dropped from the lineage, so the task releases instead of holding forever on
    an unrelated occupant."""
    import hermes_cli.kanban_db as _kb

    shared = tmp_path / "shared"
    shared.mkdir()

    root_pid = 424420
    root_starttime = 777420
    desc_pid = 900420
    desc_starttime = 777421

    state = {"root_alive": True, "desc_phase": "alive"}

    def _ident(pid):
        if pid == root_pid:
            if state["root_alive"]:
                return {"starttime": root_starttime, "cwd": str(shared), "pgid": 1}
            return None
        if pid == desc_pid:
            if state["desc_phase"] == "alive":
                return {"starttime": desc_starttime, "cwd": "/tmp", "pgid": 2}
            if state["desc_phase"] == "recycled":
                # Same PID, different starttime → recycled by an unrelated process.
                return {"starttime": 999999, "cwd": "/elsewhere", "pgid": 9}
            return None
        return None

    monkeypatch.setattr(_kb, "_read_process_identity", _ident)
    monkeypatch.setattr(
        _kb, "_discover_descendant_pids",
        lambda pid, **k: {root_pid, desc_pid},
    )

    with kb.connect() as conn:
        t = _claim_with_worker(conn, pid=root_pid)
        _record_breaker_self_fence(conn, t, shared)
        _assert_fence_lineage(
            conn, t, [{"pid": desc_pid, "starttime": desc_starttime}],
        )

    # Root gone; the captured PID is now a recycled, unrelated occupant.
    state["root_alive"] = False
    state["desc_phase"] = "recycled"

    with kb.connect() as conn:
        released = kb.release_fenced_workers(conn)
        assert t not in released
        assert kb.get_task(conn, t).status == "blocked"  # terminal, no retry


def test_snapshot_descendant_lineage_fails_closed_without_identity(monkeypatch):
    """Negative: with no root starttime identity proof (worker already gone), the
    snapshot returns an empty lineage — never a best-effort PID-only guess."""
    import hermes_cli.kanban_db as _kb

    # root_starttime is None → fail closed before any /proc walk.
    monkeypatch.setattr(
        _kb, "_discover_descendant_pids",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not walk /proc")),
    )
    assert _kb._snapshot_descendant_lineage(12345, None) == []
