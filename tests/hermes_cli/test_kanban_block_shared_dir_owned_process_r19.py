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


def _make_dir_task(
    conn, ws: Path, *, spawn_payload: dict | None = None,
    spawn_payloads: list | None = None,
) -> str:
    """Create a *claimed* ``dir``-workspace task with canonical spawn identity.

    Claims the task so it has a current run, then emits the spawn event(s)
    bound to that exact run — mirroring ``claim_task`` + ``_set_worker_pid``
    in the real dispatch path. This is what makes ``block_task``'s shared-dir
    ownership gate bind to the exact run rather than task-wide spawn history.
    """
    tid = kb.create_task(conn, title="block-dir-cleanup", assignee="a")
    conn.execute(
        "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
        (str(ws), tid),
    )
    claimed = kb.claim_task(conn, tid, claimer="host:test")
    assert claimed is not None
    run_id = claimed.current_run_id
    if spawn_payloads is not None:
        payloads = list(spawn_payloads)
    elif spawn_payload is not None:
        payloads = [spawn_payload]
    else:
        payloads = []
    for payload in payloads:
        kb._append_event(conn, tid, "spawned", payload, run_id=run_id)
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


def test_block_dir_workspace_two_same_run_spawn_identities_fails_closed(
    kanban_home, tmp_path,
):
    """Two distinct live spawn identities on one run are ambiguous — fail closed.

    A task-wide ``ORDER BY id DESC LIMIT 1`` would pick the later of the two
    identities and SIGTERM it (the auditor's exact probe). Bound to the run
    with uniqueness enforced, ownership is ambiguous, so neither same-dir
    worker may be signalled.
    """
    ws = tmp_path / "shared"
    ws.mkdir()

    worker_a = _spawn_worker(ws)
    worker_b = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        payload_a = _worker_spawn_payload(worker_a)
        payload_b = _worker_spawn_payload(worker_b)
        assert (payload_a["pid"], payload_a["starttime"]) != (
            payload_b["pid"], payload_b["starttime"],
        )

        with kb.connect() as conn:
            tid = _make_dir_task(
                conn, ws, spawn_payloads=[payload_a, payload_b],
            )
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        assert worker_a.poll() is None, (
            "worker_a signalled despite ambiguous same-run ownership"
        )
        assert worker_b.poll() is None, (
            "worker_b signalled despite ambiguous same-run ownership"
        )
    finally:
        _kill(worker_a, worker_b)


def test_block_dir_workspace_current_run_missing_spawn_with_older_history_fails_closed(
    kanban_home, tmp_path,
):
    """A current run with no spawn identity must not reuse an older run's.

    The older run leaves a live spawn identity in ``task_events``. A task-wide
    ``ORDER BY id DESC LIMIT 1`` would pick that older identity and signal the
    unrelated live worker. Bound to the exact current run (which has no spawn
    identity of its own), block_task fails closed and the worker survives.
    """
    ws = tmp_path / "shared"
    ws.mkdir()
    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        payload = _worker_spawn_payload(unrelated)

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="block-dir-cleanup", assignee="a")
            conn.execute(
                "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                (str(ws), tid),
            )
            now = int(time.time())
            # Older (ended) run whose spawn identity is the live worker.
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at, ended_at,"
                " outcome) VALUES (?, 'done', ?, ?, 'completed')",
                (tid, now - 100, now - 50),
            )
            older_run_id = cur.lastrowid
            kb._append_event(
                conn, tid, "spawned", payload, run_id=older_run_id,
            )
            # Current run with NO spawn identity of its own.
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at)"
                " VALUES (?, 'running', ?)",
                (tid, now),
            )
            current_run_id = cur.lastrowid
            conn.execute(
                "UPDATE tasks SET current_run_id = ?, status = 'running'"
                " WHERE id = ?",
                (current_run_id, tid),
            )
            conn.commit()

            assert kb.block_task(conn, tid, reason="test", kind="transient")

        assert unrelated.poll() is None, (
            "unrelated process signalled via stale older-run spawn history"
        )
    finally:
        _kill(unrelated)


def test_block_dir_workspace_stringly_identity_fails_closed(
    kanban_home, tmp_path,
):
    """A spawn identity whose pid/starttime are JSON strings is malformed.

    The committed base int()-coerced ``"<pid>"``/``"<starttime>"`` into an
    authority set, so a stringly identity that numerically matched a live
    same-dir sentinel authorized SIGTERM/-15 against it. Strict canonical
    validation treats any text (or real/bool) payload value as malformed
    evidence and refuses the whole authority set — never ``int()``-coerce.
    """
    ws = tmp_path / "shared"
    ws.mkdir()

    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        identity = kb._read_process_identity(unrelated.pid)
        assert identity is not None
        # Stringly pid/starttime that numerically match the live sentinel.
        stringly = {
            "pid": str(unrelated.pid),
            "starttime": str(identity["starttime"]),
        }

        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=stringly)
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        assert unrelated.poll() is None, (
            "stringly spawn identity authorized a signal against the "
            "same-dir sentinel"
        )
    finally:
        _kill(unrelated)


def test_block_dir_workspace_mixed_malformed_same_run_fails_closed(
    kanban_home, tmp_path,
):
    """A valid identity co-resident with an unprovable one refuses the set.

    The committed base *discarded* the unprovable row (missing/null
    ``starttime``) via ``continue`` and signalled the co-resident valid
    process. Strict validation poisons the entire exact-run authority set
    when ANY event is malformed/unprovable — never guess around it.
    """
    ws = tmp_path / "shared"
    ws.mkdir()

    worker = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        valid = _worker_spawn_payload(worker)
        # A spawn event that recorded pid but failed to record starttime.
        unprovable = {"pid": worker.pid}

        with kb.connect() as conn:
            tid = _make_dir_task(
                conn, ws, spawn_payloads=[valid, unprovable],
            )
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        assert worker.poll() is None, (
            "valid identity was signalled despite a co-resident unprovable "
            "spawn event on the same run"
        )
    finally:
        _kill(worker)


def test_block_dir_workspace_signals_ended_run_not_stale_read(
    kanban_home, tmp_path, monkeypatch,
):
    """The run actually ended inside the txn owns cleanup, not a stale pre-read.

    Reproduces the TOCTOU run-switch deterministically: the committed base
    captured ``blocked_run_id`` from ``_current_run_id`` *before* the write
    txn. A concurrent run-switch between that read and ``_end_run`` would make
    ``_end_run`` close run B while cleanup still derived ``owned_pids`` from
    the stale run A — signalling run A's old worker (SIGTERM/-15) and leaving
    run B's live worker untouched.

    Here ``_current_run_id`` is monkeypatched to report the stale run A, but
    the fixed ``block_task`` never consults it up front — it uses the run
    ``_end_run`` actually ends inside the transaction (run B). Run B's worker
    is signalled; run A's worker survives.
    """
    ws = tmp_path / "shared"
    ws.mkdir()

    worker_a = _spawn_worker(ws)
    worker_b = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        payload_a = _worker_spawn_payload(worker_a)
        payload_b = _worker_spawn_payload(worker_b)

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="block-dir-cleanup", assignee="a")
            conn.execute(
                "UPDATE tasks SET workspace_kind='dir', workspace_path=? "
                "WHERE id=?",
                (str(ws), tid),
            )
            # Current run B (the run that will actually be ended inside the
            # txn), claimed first so the predecessor-exit guard doesn't block.
            claimed = kb.claim_task(conn, tid, claimer="host:test")
            assert claimed is not None
            run_b = claimed.current_run_id
            kb._append_event(conn, tid, "spawned", payload_b, run_id=run_b)
            # Stale older run A with its own distinct (live) worker identity.
            now = int(time.time())
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at, ended_at,"
                " outcome) VALUES (?, 'done', ?, ?, 'completed')",
                (tid, now - 100, now - 50),
            )
            run_a = cur.lastrowid
            kb._append_event(conn, tid, "spawned", payload_a, run_id=run_a)
            conn.commit()

            # Simulate the stale pre-txn read reporting run A. The fixed
            # block_task never reads _current_run_id up front; it derives
            # ownership from the run _end_run actually ended (run B).
            monkeypatch.setattr(kb, "_current_run_id", lambda c, t: run_a)
            assert kb.block_task(conn, tid, reason="test", kind="transient")

        worker_b.wait(timeout=5)
        assert worker_b.returncode != 0, (
            "current run's worker was not signalled (stale run identity used)"
        )
        assert worker_a.poll() is None, (
            "stale run's worker was signalled by block_task"
        )
    finally:
        _kill(worker_a, worker_b)


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
