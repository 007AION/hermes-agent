"""Tests for the post-terminal / closed-workspace mutation guard (AION-RL2-CORE-01-R19).

Fences the defect where a terminal task's scratch workspace is removed on
completion, but a stale/null/cross-task session (a long-lived gateway
conversation, a cron tick, or a lingering worker) can still mutate it through
``write_file``/``patch``/``terminal``/checkpoint and silently recreate the
directory.

GREEN contract (frozen R19 factory truth):

    * a mutation into a terminal (``done``|``archived``) task's managed scratch
      workspace is refused with a deterministic receipt and zero recreation;
    * ``failed`` and ``cancelled`` are NOT terminal: their still-extant
      workspace is not fenced by terminality alone;
    * an active task's workspace is mutable ONLY by a proven legitimate
      same-task writer (matching task + run + PID + starttime where recorded);
    * a null / cross-task / stale-run / mismatched-PID / recycled-PID writer
      fails closed against an active task's managed workspace;
    * an active task whose scratch workspace was closed / deleted / points
      elsewhere fails closed;
    * a non-managed path is allowed (no overblocking);
    * managed-root discovery / readability ambiguity fails closed when a
      kanban context is active, and never overblocks ordinary non-managed paths.

Every fixture pins an isolated temp DB/root via ``isolated_kanban_env`` and
mechanically asserts it is not the live aion-factory board.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

_LIVE_AION_DB = Path("/root/.hermes/kanban/boards/aion-factory/kanban.db")


@pytest.fixture
def kanban_env(tmp_path):
    with kb.isolated_kanban_env(tmp_path):
        kb.init_db()
        # Mechanical isolation proof: the guard/tools must resolve to the temp
        # root, never the live aion-factory board.
        assert kb.kanban_db_path() != _LIVE_AION_DB
        assert str(tmp_path) in str(kb.kanban_db_path())
        yield tmp_path


def _make_scratch_task(conn) -> tuple[str, Path]:
    tid = kb.create_task(conn, title="guard-task", assignee="w")
    task = kb.get_task(conn, tid)
    assert task is not None
    ws = kb.resolve_workspace(task)
    kb.set_workspace_path(conn, tid, ws)
    assert ws.is_dir()
    return tid, ws


def _bind_worker(conn, tid, *, run_id=1, pid=None, starttime=None):
    """Simulate a dispatcher-spawned worker identity on the task row."""
    pid = os.getpid() if pid is None else pid
    starttime = kb._process_starttime() if starttime is None else starttime
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_starttime=?, current_run_id=? WHERE id=?",
        (pid, starttime, run_id, tid),
    )
    conn.commit()


def _bind_env(monkeypatch, tid, run_id="1"):
    """Simulate the worker's dispatcher-injected identity env pins."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)


def _write_file_tool(path: str, content: str = "x = 1\n"):
    from tools.file_tools import write_file_tool

    return write_file_tool(path, content)


def _patch_tool(path: str):
    from tools.file_tools import patch_tool

    return patch_tool(mode="replace", path=path, old_string="x = 1", new_string="x = 2")


def _assert_refusal(refusal, *, task_id, status, detail, workspace=None):
    assert refusal is not None
    assert refusal["refused"] is True
    assert refusal["reason"] == "terminal_task_workspace"
    assert refusal["task_id"] == task_id
    assert refusal["task_status"] == status
    assert refusal["detail"] == detail
    if workspace is not None:
        assert refusal["workspace"] == workspace


# ── core terminality (kanban_db level) ──────────────────────────────────────


def test_terminal_scratch_workspace_write_is_refused(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        assert kb.complete_task(conn, tid, result="done")
        assert not ws.exists(), "scratch workspace should be removed on completion"

    _assert_refusal(
        kb.terminal_workspace_write_refusal(str(ws / "new_file.py")),
        task_id=tid, status="done", detail="terminal_task", workspace=str(ws),
    )


def test_archived_scratch_workspace_write_is_refused(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("archived", tid))
        conn.commit()

    _assert_refusal(
        kb.terminal_workspace_write_refusal(str(ws / "x.py")),
        task_id=tid, status="archived", detail="terminal_task", workspace=str(ws),
    )


def test_unknown_task_id_under_managed_root_fails_closed(kanban_env):
    root = kb.workspaces_root()
    bogus = root / "t_deadbeef" / "x.py"
    _assert_refusal(
        kb.terminal_workspace_write_refusal(str(bogus)),
        task_id="t_deadbeef", status=None, detail="unknown_task",
        workspace=str(root / "t_deadbeef"),
    )


def test_malformed_task_id_under_managed_root_fails_closed(kanban_env):
    root = kb.workspaces_root()
    _assert_refusal(
        kb.terminal_workspace_write_refusal(str(root / "README.md")),
        task_id="README.md", status=None, detail="malformed_task_id",
        workspace=str(root / "README.md"),
    )


def test_non_managed_path_is_allowed(kanban_env, tmp_path):
    project = tmp_path / "code" / "proj"
    project.mkdir(parents=True)
    assert kb.terminal_workspace_write_refusal(str(project / "x.py")) is None


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled_are_not_terminal_by_status(kanban_env, status):
    # failed/cancelled are reclaimable; with a legitimate same-task writer the
    # still-extant workspace is NOT fenced by terminality alone.
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=None,
            writer_pid=12345,
        ),
        task_id=tid, status=status, detail="identity_null", workspace=str(ws),
    )


# ── active-task ownership + identity binding (F2) ────────────────────────────


def test_active_workspace_allowed_for_legitimate_same_task_writer(kanban_env, monkeypatch):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=os.getpid(), starttime=kb._process_starttime())

    _bind_env(monkeypatch, tid, run_id="1")
    # Guard resolves writer identity from env/process — same task, run, pid, starttime.
    assert kb.terminal_workspace_write_refusal(str(ws / "ok.py")) is None


def test_null_writer_to_active_workspace_fails_closed(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1)

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=None,
            writer_pid=12345,
        ),
        task_id=tid, status="ready", detail="identity_null", workspace=str(ws),
    )


def test_cross_task_writer_to_active_workspace_fails_closed(kanban_env):
    with kb.connect() as conn:
        tid_a, ws_a = _make_scratch_task(conn)
        _bind_worker(conn, tid_a, run_id=1)
        tid_b, _ = _make_scratch_task(conn)

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws_a / "x.py"),
            writer_task=tid_b,
            writer_pid=os.getpid(),
        ),
        task_id=tid_a, status="ready", detail="identity_cross_task", workspace=str(ws_a),
    )


def test_stale_run_writer_to_active_workspace_fails_closed(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=7)

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id="99",  # stale — no longer the current run
            writer_pid=os.getpid(),
        ),
        task_id=tid, status="ready", detail="identity_stale_run", workspace=str(ws),
    )


def test_pid_mismatch_writer_fails_closed(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=4242, starttime=111)

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id="1",
            writer_pid=9999,  # mismatched PID — stale/replayed worker
            writer_starttime=111,
        ),
        task_id=tid, status="ready", detail="identity_pid_mismatch", workspace=str(ws),
    )


def test_starttime_mismatch_writer_fails_closed(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=4242, starttime=111)

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id="1",
            writer_pid=4242,
            writer_starttime=222,  # recycled PID — same pid, different starttime
        ),
        task_id=tid, status="ready", detail="identity_starttime_mismatch",
        workspace=str(ws),
    )


def test_active_task_with_closed_workspace_fails_closed(kanban_env):
    """An active task whose scratch workspace was deleted out from under it must
    not have the write recreate the directory (closed-workspace state, F2)."""
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=os.getpid(), starttime=kb._process_starttime())
        import shutil
        shutil.rmtree(ws)
        assert not ws.exists()

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id="1",
            writer_pid=os.getpid(),
            writer_starttime=kb._process_starttime(),
        ),
        task_id=tid, status="ready", detail="workspace_closed", workspace=str(ws),
    )


def test_ready_unowned_task_with_matching_writer_task_fails_closed(kanban_env):
    """F1: a ready/unowned task (null run/PID/starttime) must NOT authorize
    mutation even when ``writer_task`` matches — the old guard's unconditional
    allow reached by matching writer_task alone is now a refusal."""
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        # no _bind_worker — ownership stays null (ready, unowned)

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id="replayed-run",
            writer_pid=os.getpid(),
            writer_starttime=kb._process_starttime(),
        ),
        task_id=tid, status="ready", detail="identity_unowned", workspace=str(ws),
    )
    assert not (ws / "x.py").exists()


@pytest.mark.parametrize("status", ["todo", "blocked"])
def test_unowned_task_with_cleared_ownership_fails_closed(kanban_env, status):
    """F2: todo/blocked tasks with cleared ownership fail closed, not just
    ready (the auditor probed ready/todo/blocked with cleared ownership)."""
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id="replayed-run",
            writer_pid=os.getpid(),
            writer_starttime=kb._process_starttime(),
        ),
        task_id=tid, status=status, detail="identity_unowned", workspace=str(ws),
    )


def test_null_run_writer_fails_closed(kanban_env):
    """F1: a null writer run must not be accepted against a recorded current
    run (previously only non-null-and-unequal runs were rejected)."""
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=4242, starttime=111)

    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id=None,  # null run against a recorded current run
            writer_pid=4242,
            writer_starttime=111,
        ),
        task_id=tid, status="ready", detail="identity_null_run", workspace=str(ws),
    )


def test_null_starttime_writer_fails_closed(kanban_env, monkeypatch):
    """F1: an unreadable/null writer starttime must not be accepted against a
    recorded starttime (previously only non-null-and-unequal values rejected)."""
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=4242, starttime=111)

    # Simulate an unreadable /proc starttime (non-Linux host).
    monkeypatch.setattr(kb, "_process_starttime", lambda: None)
    _assert_refusal(
        kb.terminal_workspace_write_refusal(
            str(ws / "x.py"),
            writer_task=tid,
            writer_run_id="1",
            writer_pid=4242,
            writer_starttime=None,  # null starttime against a recorded value
        ),
        task_id=tid, status="ready", detail="identity_null_starttime",
        workspace=str(ws),
    )


def test_writer_session_parameter_removed(kanban_env):
    """F1: the unsupported ``writer_session`` parameter was removed — the guard
    binds run/PID/starttime identity, not an unused session argument."""
    import inspect

    params = inspect.signature(kb.terminal_workspace_write_refusal).parameters
    assert "writer_session" not in params
    assert "writer_run_id" in params
    assert "writer_starttime" in params


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_and_cancelled_reclaimable_with_current_run(kanban_env, monkeypatch, status):
    """F2: failed/cancelled are reclaimable — a re-dispatched run (fresh
    run/PID/starttime) may still mutate the extant workspace; terminality alone
    does not fence it (positive path the old test never exercised)."""
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=2, pid=os.getpid(),
                     starttime=kb._process_starttime())
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()

    _bind_env(monkeypatch, tid, run_id="2")
    assert kb.terminal_workspace_write_refusal(str(ws / "ok.py")) is None


def test_replayed_refusal_is_deterministic_and_idempotent(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        assert kb.complete_task(conn, tid, result="done")
        assert not ws.exists()

    first = kb.terminal_workspace_write_refusal(str(ws / "r.py"))
    for _ in range(3):
        assert kb.terminal_workspace_write_refusal(str(ws / "r.py")) == first
    assert not ws.exists()


def test_null_and_unparseable_paths_do_not_raise(kanban_env):
    assert kb.terminal_workspace_write_refusal(None) is None
    assert kb.terminal_workspace_write_refusal("") is None


# ── managed-root discovery / readability fail-closed (F7) ───────────────────


def test_discovery_exception_fails_closed_when_context_active(kanban_env, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_c0ffee00")
    monkeypatch.setattr(
        kb, "_managed_scratch_path_info_strict",
        lambda p: (_ for _ in ()).throw(RuntimeError("root discovery failed")),
    )
    refusal = kb.terminal_workspace_write_refusal("/root/.hermes/kanban/workspaces/t_c0ffee00/x.py")
    assert refusal is not None
    assert refusal["refused"] is True
    assert refusal["detail"].startswith("discovery_failed")


def test_discovery_exception_allows_when_no_context(tmp_path, monkeypatch):
    # With no kanban context, an unexpected discovery failure is treated as
    # non-managed (no overblocking of ordinary writes).
    for k in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT",
              "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(
        kb, "_managed_scratch_path_info_strict",
        lambda p: (_ for _ in ()).throw(RuntimeError("root discovery failed")),
    )
    assert kb.terminal_workspace_write_refusal(str(tmp_path / "x.py")) is None


def test_kanban_home_unreadable_fails_closed_when_context_active(kanban_env, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_c0ffee00")
    monkeypatch.setattr(kb, "kanban_home", lambda: (_ for _ in ()).throw(OSError("home unreadable")))
    # HERMES_KANBAN_WORKSPACES_ROOT is unset here so the override root is absent;
    # with kanban_home raising, managed-ness cannot be ruled out -> fail closed.
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    refusal = kb.terminal_workspace_write_refusal("/root/.hermes/kanban/workspaces/t_c0ffee00/x.py")
    assert refusal is not None
    assert refusal["detail"].startswith("discovery_ambiguous")


# ── end-to-end file-mutation refusal ────────────────────────────────────────


def test_write_file_tool_refuses_terminal_workspace_and_does_not_recreate(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        assert kb.complete_task(conn, tid, result="done")
        assert not ws.exists()

    result = _write_file_tool(str(ws / "resurrected.py"))
    payload = json.loads(result)
    assert payload.get("error")
    assert payload.get("refused") is True
    assert payload.get("reason") == "terminal_task_workspace"
    assert payload.get("detail") == "terminal_task"
    assert payload.get("task_id") == tid
    assert not ws.exists(), "refused write must NOT recreate the workspace"


def test_write_file_tool_refuses_unknown_task_and_does_not_recreate(kanban_env):
    root = kb.workspaces_root()
    target = root / "t_c0ffee00" / "x.py"
    result = _write_file_tool(str(target))
    payload = json.loads(result)
    assert payload.get("error")
    assert payload.get("refused") is True
    assert payload.get("task_id") == "t_c0ffee00"
    assert not (root / "t_c0ffee00").exists(), \
        "refused unknown-task write must NOT create the directory"


def test_write_file_tool_allows_active_same_task_workspace(kanban_env, monkeypatch):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=os.getpid(), starttime=kb._process_starttime())

    _bind_env(monkeypatch, tid, run_id="1")
    result = _write_file_tool(str(ws / "ok.py"))
    payload = json.loads(result)
    assert not payload.get("error")
    assert (ws / "ok.py").is_file()


def test_patch_tool_refuses_terminal_workspace(kanban_env):
    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        assert kb.complete_task(conn, tid, result="done")
        assert not ws.exists()

    result = _patch_tool(str(ws / "victim.py"))
    payload = json.loads(result)
    assert payload.get("error")
    assert payload.get("refused") is True
    assert payload.get("reason") == "terminal_task_workspace"
    assert not ws.exists(), "refused patch must NOT recreate the workspace"


# ── terminal cwd fence (F1) ─────────────────────────────────────────────────


def test_terminal_cwd_guard_refuses_terminal_workspace(kanban_env):
    from tools.terminal_tool import _terminal_workspace_cwd_guard

    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        assert kb.complete_task(conn, tid, result="done")
        assert not ws.exists()

    blocked = _terminal_workspace_cwd_guard(str(ws))
    assert blocked is not None
    payload = json.loads(blocked)
    assert payload["status"] == "blocked"
    assert payload["refused"] is True
    assert payload["detail"] == "terminal_task"
    assert payload["task_id"] == tid


def test_terminal_cwd_guard_allows_active_same_task_workspace(kanban_env, monkeypatch):
    from tools.terminal_tool import _terminal_workspace_cwd_guard

    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        _bind_worker(conn, tid, run_id=1, pid=os.getpid(), starttime=kb._process_starttime())

    _bind_env(monkeypatch, tid, run_id="1")
    assert _terminal_workspace_cwd_guard(str(ws)) is None


def test_terminal_cwd_guard_allows_non_managed_path(kanban_env, tmp_path):
    from tools.terminal_tool import _terminal_workspace_cwd_guard

    proj = tmp_path / "proj"
    proj.mkdir()
    assert _terminal_workspace_cwd_guard(str(proj)) is None


# ── checkpoint fence (F1) ───────────────────────────────────────────────────


def test_checkpoint_workspace_fenced_for_terminal_workspace(kanban_env):
    from tools.checkpoint_manager import _checkpoint_workspace_fenced

    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        assert kb.complete_task(conn, tid, result="done")

    assert _checkpoint_workspace_fenced(str(ws)) is True


def test_checkpoint_not_fenced_for_non_managed_path(kanban_env, tmp_path):
    from tools.checkpoint_manager import _checkpoint_workspace_fenced

    proj = tmp_path / "proj"
    proj.mkdir()
    assert _checkpoint_workspace_fenced(str(proj)) is False


def test_checkpoint_manager_skips_terminal_workspace(kanban_env):
    from tools.checkpoint_manager import CheckpointManager

    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)
        assert kb.complete_task(conn, tid, result="done")

    mgr = CheckpointManager(enabled=True)
    assert mgr.ensure_checkpoint(str(ws), reason="test") is False


def test_checkpoint_guard_exception_fails_closed_when_context_active(kanban_env, monkeypatch):
    """F3: an unexpected guard exception in an active Kanban context must fail
    closed (fenced), not authorize the checkpoint mutation."""
    from tools.checkpoint_manager import _checkpoint_workspace_fenced

    with kb.connect() as conn:
        tid, ws = _make_scratch_task(conn)

    monkeypatch.setattr(
        kb, "terminal_workspace_write_refusal",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("guard exploded")),
    )
    assert _checkpoint_workspace_fenced(str(ws)) is True


def test_checkpoint_guard_exception_allows_when_no_context(tmp_path, monkeypatch):
    """F3: without a Kanban context, an unexpected guard exception is treated
    as non-managed (no overblocking of ordinary checkpoints)."""
    from tools.checkpoint_manager import _checkpoint_workspace_fenced

    for k in ("HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT",
              "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(
        kb, "terminal_workspace_write_refusal",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("guard exploded")),
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    assert _checkpoint_workspace_fenced(str(proj)) is False
