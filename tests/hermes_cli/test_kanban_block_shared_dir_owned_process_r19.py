"""Isolated RED→GREEN tests for AION-RL2-CORE-01 block_task shared-dir ownership.

The bug. ``block_task`` (the typed-block path) closes the task's workspace
processes with ``close_workspace_processes(_wp)`` — i.e. ``owned_pids=None``.
The cleanup API documents that ``owned_pids=None`` makes cwd containment the
sole signalling authority, which is only correct for exclusive ``scratch`` /
``worktree`` workspaces. For a ``workspace_kind=dir`` task sharing a directory
with unrelated workers (the Factory Director, another GM, an evidence command),
a typed block therefore SIGTERMs every process whose cwd resolves inside the
shared directory. Canonical incident t_e690dcc1's bounded repair child
(t_eac7bf33): source task t_fdd4aa25 run4053 entered a typed transient block
and an unrelated Factory Director foreground diagnostic with cwd
``/root/aion-governance`` died by SIGTERM (exit -15) in the same interval.

The repair. ``block_task`` must read ``workspace_kind`` alongside
``workspace_path`` and, for ``workspace_kind=dir``, derive the exact task/run
owned worker lineage (worker PID + descendants) from the canonical ``spawned``
event (``pid`` + ``/proc`` ``starttime``) and pass it as ``owned_pids`` — never
cwd-only authority. When the canonical owned lineage is absent or ambiguous
(missing/legacy/malformed spawn identity, recycled PID, exited worker), it
fails closed: no shared-dir processes are signalled.

Contract under test:

* ``block_task`` returns True (block succeeds) but must never signal an
  unrelated same-directory process on a ``dir`` workspace.
* ``block_task`` calls ``close_workspace_processes`` with a non-None
  ``owned_pids`` for ``dir`` workspaces (never cwd-only authority).
* the exact owned worker (and its descendants) still close under
  PID/starttime/TOCTOU and bounded TERM→KILL guards.
* scratch workspace exclusivity is unchanged (cwd containment still applies).
* shared-dir behaviour fails closed when the canonical owned lineage is absent
  or ambiguous.

All tests are hermetic under a dispatcher-pinned environment (``kanban_home``
+ ``isolated_kanban_env``) and spawn real OS processes against an isolated
temporary DB — never the live board, never the shared ``/root/aion-governance``
cwd.

File: tests/hermes_cli/test_kanban_block_shared_dir_owned_process_r19.py
"""

from __future__ import annotations

import subprocess
import sys
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


def _spawn_worker(ws: Path, *, new_session: bool = True) -> subprocess.Popen:
    """Spawn a ``sleep 300`` worker whose cwd is inside *ws*."""
    return subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=new_session,
    )


def _worker_spawn_payload(proc: subprocess.Popen) -> dict:
    """Build a canonical ``spawned`` payload from a live worker process."""
    identity = kb._read_process_identity(proc.pid)
    assert identity is not None
    return {"pid": proc.pid, "starttime": identity["starttime"]}


def _make_dir_task(conn, ws: Path, *, spawn_payload: dict | None = None) -> str:
    """Create a blockable ``dir``-workspace task, optionally with a spawned event."""
    tid = kb.create_task(conn, title="block-dir-cleanup", assignee="a")
    conn.execute(
        "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
        (str(ws), tid),
    )
    if spawn_payload is not None:
        kb._append_event(conn, tid, "spawned", spawn_payload)
    conn.commit()
    return tid


def _make_scratch_task(conn, ws: Path) -> str:
    tid = kb.create_task(conn, title="block-scratch-cleanup", assignee="a")
    conn.execute(
        "UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
        (str(ws), tid),
    )
    conn.commit()
    return tid


def _kill(*procs):
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=2)
        except Exception:
            pass


# ── Shared-dir ownership gate (the RED→GREEN) ──────────────────────────────


def test_block_dir_workspace_preserves_unrelated_same_dir_process(
    kanban_home, tmp_path,
):
    """Owned worker is closed; an unrelated same-dir worker survives the block.

    On the current base this is RED: ``block_task`` calls
    ``close_workspace_processes(_wp)`` with cwd-only authority, so the
    unrelated sibling is SIGTERM'd too. After the repair it derives
    ``owned_pids`` from the canonical ``spawned`` event and skips the
    unrelated process as unowned.
    """
    ws = tmp_path / "shared"
    ws.mkdir()

    worker = _spawn_worker(ws)
    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        payload = _worker_spawn_payload(worker)

        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=payload)
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        worker.wait(timeout=5)
        assert worker.returncode != 0, "owned worker was not signalled"
        assert unrelated.poll() is None, (
            "unrelated same-dir worker was signalled by block_task"
        )
    finally:
        _kill(worker, unrelated)


def test_block_dir_workspace_never_passes_cwd_only_authority(
    kanban_home, tmp_path, monkeypatch,
):
    """block_task must pass a non-None owned_pids for a dir workspace.

    ``owned_pids=None`` is cwd-only authority — correct only for exclusive
    scratch/worktree workspaces. For ``workspace_kind=dir`` the exact
    task/run owned lineage (worker PID + descendants) must be passed, and
    the unrelated sibling must not be in it.
    """
    ws = tmp_path / "shared"
    ws.mkdir()

    worker = _spawn_worker(ws)
    unrelated = _spawn_worker(ws)
    calls: list = []
    orig = kb.close_workspace_processes

    def _spy(path, **kw):
        calls.append((path, kw.get("owned_pids")))
        return orig(path, **kw)

    monkeypatch.setattr(kb, "close_workspace_processes", _spy)
    try:
        time.sleep(0.1)
        payload = _worker_spawn_payload(worker)

        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=payload)
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        assert calls, "block_task did not attempt workspace cleanup"
        _path, owned = calls[0]
        assert owned is not None, (
            "block_task passed cwd-only authority (owned_pids=None) for a "
            "workspace_kind=dir task"
        )
        assert worker.pid in owned, "owned worker missing from owned_pids"
        assert unrelated.pid not in owned, (
            "unrelated same-dir worker leaked into owned_pids"
        )
    finally:
        _kill(worker, unrelated)


def test_block_dir_workspace_no_spawn_identity_fails_closed(
    kanban_home, tmp_path,
):
    """A dir task with no spawn event signals nothing (fail closed)."""
    ws = tmp_path / "shared"
    ws.mkdir()

    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=None)
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        assert unrelated.poll() is None, (
            "unrelated process was signalled despite no spawn identity"
        )
    finally:
        _kill(unrelated)


def test_block_dir_workspace_recycled_pid_fails_closed(kanban_home, tmp_path):
    """A live PID whose starttime differs from the spawn event is refused."""
    ws = tmp_path / "shared"
    ws.mkdir()

    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        identity = kb._read_process_identity(unrelated.pid)
        assert identity is not None
        wrong_starttime = identity["starttime"] + 999

        with kb.connect() as conn:
            tid = _make_dir_task(
                conn, ws,
                spawn_payload={"pid": unrelated.pid, "starttime": wrong_starttime},
            )
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        assert unrelated.poll() is None, (
            "recycled PID was signalled despite starttime mismatch"
        )
    finally:
        _kill(unrelated)


# ── Descendant closure (owned child/descendant still closes) ───────────────


@pytest.mark.live_system_guard_bypass
def test_block_dir_workspace_closes_descendant_not_unrelated(
    kanban_home, tmp_path,
):
    """Owned worker + its grandchild are signalled; unrelated sibling survives.

    Marked ``live_system_guard_bypass``: the worker is signalled first (lower
    PID), so the grandchild is reparented to init before it is signalled — real
    signal delivery to a genuinely owned tree whose parent chain no longer
    includes the test process.
    """
    ws = tmp_path / "shared"
    ws.mkdir()

    worker = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, time; "
         "c = subprocess.Popen(['sleep', '300']); time.sleep(300)"],
        cwd=str(ws),
        start_new_session=True,
    )
    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.3)
        payload = _worker_spawn_payload(worker)

        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=payload)
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        worker.wait(timeout=8)
        assert worker.returncode != 0, "owned worker was not signalled"
        assert unrelated.poll() is None, (
            "unrelated same-dir worker was signalled by block_task"
        )
    finally:
        _kill(worker, unrelated)


# ── Scratch exclusivity is unchanged ────────────────────────────────────────


def test_block_scratch_workspace_still_cwd_containment(kanban_home, tmp_path):
    """Scratch workspace still closes in-workspace processes (cwd containment).

    Exclusivity semantics must remain correct: scratch is exclusive to one
    task, so a same-dir process is owned by definition and is signalled.
    """
    ws = tmp_path / "ws"
    ws.mkdir()

    child = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        with kb.connect() as conn:
            tid = _make_scratch_task(conn, ws)
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        child.wait(timeout=5)
        assert child.returncode != 0, "scratch in-workspace worker was not signalled"
    finally:
        _kill(child)
