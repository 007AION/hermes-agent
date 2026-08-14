"""Isolated RED\u2192GREEN tests for AION-RL2-CORE-01-R17 dir-workspace spawn identity repair.

The bug. PR #6 (AION-CORE-PR6, gate-epoch CAS) added
``_cleanup_workspace_on_completion``, a second completion-time process
closure that derives the shared-dir (``workspace_kind=dir``) ``owned_pids``
gate from a ``task_spawns`` table. That table does not exist in the
canonical schema: ``init_db`` only ever creates ``tasks``, ``task_links``,
``task_comments``, ``task_events``, ``task_runs``, ``gate_epochs``,
``task_attachments`` and ``kanban_notify_subs``. So every dir-workspace
completion raised ``sqlite3.OperationalError: no such table: task_spawns``
inside the function's ``try/except``, was logged as ``cleanup failed`` and
silently skipped every process closure. The activation task t_6e28b044
emitted exactly that line.

The repair. The canonical spawn identity already lives in ``task_events``:
``_set_worker_pid`` records a ``spawned`` event whose payload carries
``pid`` + ``/proc`` ``starttime``, and ``_cleanup_workspace`` already reads
that same event to gate shared-dir ownership. ``_cleanup_workspace_on_completion``
must derive identity from that same canonical source and never query a
nonexistent table. The fail-closed fence is unchanged in shape: only a
spawn event with BOTH a valid ``pid`` AND a well-formed ``starttime`` can
prove ownership; descendants are re-discovered with ``expected_starttime``
so a recycled PID is rejected; missing/legacy/malformed starttime and a
bare-PID fallback never authorize a signal.

Contract under test:

* ``_cleanup_workspace_on_completion`` returns a deterministic evidence
  dict whose ``outcome`` is one of ``success``, ``safe_refusal``,
  ``identity_mismatch``, ``no_task``, ``no_workspace`` or
  ``internal_error`` \u2014 never ``None``, and never raises.
* dir-workspace closure signals only the exact task-owned worker + its
  revalidated descendants; unrelated same-directory processes, missing
  identity, stale identity and PID-reuse mismatches are never signalled.
* scratch/worktree closure is unchanged (cwd containment, no ownership gate).
* completion remains terminal even when cleanup fails.

All tests are hermetic under a dispatcher-pinned environment (``kanban_home``
+ ``isolated_kanban_env``) and spawn real OS processes against an isolated
temporary DB \u2014 never the live board.

File: tests/hermes_cli/test_kanban_dir_workspace_spawn_identity_r17.py
"""

from __future__ import annotations

import os
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
    """Create a dir-workspace task, optionally attaching a spawned event."""
    tid = kb.create_task(conn, title="r17-dir-cleanup", assignee="a")
    conn.execute(
        "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
        (str(ws), tid),
    )
    if spawn_payload is not None:
        kb._append_event(conn, tid, "spawned", spawn_payload)
    conn.commit()
    return tid


# ── Canonical schema / RED ──────────────────────────────────────────────────


def test_canonical_schema_has_no_task_spawns_table(kanban_home):
    """The live schema must not contain ``task_spawns`` \u2014 canonical identity
    lives in ``task_events``, and no migration ever added a spawns table."""
    with kb.connect() as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "task_events" in tables
    assert "task_runs" in tables
    assert "task_spawns" not in tables


def test_dir_completion_closes_owned_worker_via_canonical_spawn_identity(
    kanban_home, tmp_path,
):
    """RED\u2192GREEN: dir completion closes the owned worker from task_events.

    On the current base this is RED: ``_cleanup_workspace_on_completion``
    queries ``task_spawns``, raises ``no such table`` internally and returns
    ``None``, leaving the owned worker alive. After the repair it derives
    the worker from the canonical ``spawned`` event and signals it.
    """
    ws = tmp_path / "ws"
    ws.mkdir()

    worker = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        payload = _worker_spawn_payload(worker)

        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=payload)
            result = kb._cleanup_workspace_on_completion(conn, tid)

        # Deterministic evidence contract, not a silent None.
        assert result is not None, (
            "cleanup returned None \u2014 likely the task_spawns no-such-table "
            "path was hit and swallowed"
        )
        assert result["outcome"] == "success"
        assert result["evidence"]["signalled"] >= 1

        worker.wait(timeout=5)
        assert worker.returncode != 0, "owned worker was not signalled"
    finally:
        try:
            worker.kill()
            worker.wait(timeout=2)
        except Exception:
            pass


@pytest.mark.live_system_guard_bypass
def test_dir_completion_closes_descendant_process(kanban_home, tmp_path):
    """A grandchild descendant of the owned worker is also signalled.

    Marked ``live_system_guard_bypass``: the worker is signalled first (its
    PID is lower), so the grandchild is reparented to init before it is
    signalled \u2014 real signal delivery to a genuinely owned tree.
    """
    ws = tmp_path / "ws"
    ws.mkdir()

    worker = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, time; "
         "c = subprocess.Popen(['sleep', '300']); time.sleep(300)"],
        cwd=str(ws),
        start_new_session=True,
    )
    try:
        time.sleep(0.3)
        payload = _worker_spawn_payload(worker)

        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=payload)
            result = kb._cleanup_workspace_on_completion(conn, tid)

        assert result["outcome"] == "success"
        # worker + its grandchild were both signalled.
        assert result["evidence"]["signalled"] >= 2
    finally:
        try:
            worker.kill()
            worker.wait(timeout=2)
        except Exception:
            pass


def test_dir_completion_preserves_unrelated_same_dir_worker(
    kanban_home, tmp_path,
):
    """Owned worker is closed; an unrelated same-dir worker is preserved.

    ``unrelated`` is a sibling of the owned worker (both children of the
    test process), not a descendant of the owned worker's PID \u2014 so it must
    be skipped as unowned even though it shares the directory.
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
            result = kb._cleanup_workspace_on_completion(conn, tid)

        assert result["outcome"] == "success"
        assert result["evidence"]["skipped_unowned"] >= 1
        worker.wait(timeout=5)
        assert worker.returncode != 0
        assert unrelated.poll() is None, (
            "unrelated same-dir worker was signalled"
        )
    finally:
        for p in [worker, unrelated]:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


def test_dir_completion_no_spawn_legacy_task_safe_refusal(kanban_home, tmp_path):
    """A dir task with no spawn event refuses to signal anything."""
    ws = tmp_path / "shared"
    ws.mkdir()

    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=None)
            result = kb._cleanup_workspace_on_completion(conn, tid)

        assert result["outcome"] == "safe_refusal"
        assert result["reason"] == "no_provable_spawn_identity"
        assert unrelated.poll() is None, (
            "unrelated process was signalled despite no spawn identity"
        )
    finally:
        try:
            unrelated.kill()
            unrelated.wait(timeout=2)
        except Exception:
            pass


def test_dir_completion_legacy_missing_starttime_safe_refusal(
    kanban_home, tmp_path,
):
    """A legacy spawn event (PID only, no starttime) fails closed."""
    ws = tmp_path / "shared"
    ws.mkdir()

    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        with kb.connect() as conn:
            # Legacy spawn event: pid only, no starttime key.
            tid = _make_dir_task(
                conn, ws,
                spawn_payload={"pid": unrelated.pid},
            )
            result = kb._cleanup_workspace_on_completion(conn, tid)

        assert result["outcome"] == "safe_refusal"
        assert result["reason"] == "no_provable_spawn_identity"
        assert unrelated.poll() is None, (
            "legacy spawn without starttime authorised a signal"
        )
    finally:
        try:
            unrelated.kill()
            unrelated.wait(timeout=2)
        except Exception:
            pass


def test_dir_completion_recycled_pid_identity_mismatch(kanban_home, tmp_path):
    """A live process whose PID matches the spawn event but whose starttime
    differs (recycled PID) is refused with an identity-mismatch outcome."""
    ws = tmp_path / "shared"
    ws.mkdir()

    unrelated = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        unrelated_identity = kb._read_process_identity(unrelated.pid)
        assert unrelated_identity is not None
        wrong_starttime = unrelated_identity["starttime"] + 999

        with kb.connect() as conn:
            tid = _make_dir_task(
                conn, ws,
                spawn_payload={
                    "pid": unrelated.pid,
                    "starttime": wrong_starttime,
                },
            )
            result = kb._cleanup_workspace_on_completion(conn, tid)

        assert result["outcome"] == "identity_mismatch"
        assert result["reason"] == "recycled_pid"
        assert unrelated.poll() is None, (
            "recycled PID was signalled despite starttime mismatch"
        )
    finally:
        try:
            unrelated.kill()
            unrelated.wait(timeout=2)
        except Exception:
            pass


def test_dir_completion_repeated_is_idempotent(kanban_home, tmp_path):
    """Calling cleanup twice is a safe no-op the second time."""
    ws = tmp_path / "ws"
    ws.mkdir()

    worker = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        payload = _worker_spawn_payload(worker)

        with kb.connect() as conn:
            tid = _make_dir_task(conn, ws, spawn_payload=payload)
            first = kb._cleanup_workspace_on_completion(conn, tid)
            second = kb._cleanup_workspace_on_completion(conn, tid)

        assert first is not None and second is not None
        assert first["outcome"] == "success"
        # Second run is a no-op (worker already gone) and never raises.
        assert second["outcome"] in ("success", "safe_refusal")
    finally:
        try:
            worker.kill()
            worker.wait(timeout=2)
        except Exception:
            pass


def test_scratch_completion_still_closes_without_ownership(kanban_home, tmp_path):
    """Scratch workspace closes in-workspace processes without ownership gating."""
    ws = tmp_path / "ws"
    ws.mkdir()

    child = _spawn_worker(ws)
    try:
        time.sleep(0.1)
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="r17-scratch", assignee="a")
            conn.execute(
                "UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
                (str(ws), tid),
            )
            conn.commit()
            result = kb._cleanup_workspace_on_completion(conn, tid)

        assert result["outcome"] == "success"
        child.wait(timeout=5)
        assert child.returncode != 0
    finally:
        try:
            child.kill()
            child.wait(timeout=2)
        except Exception:
            pass


def test_completion_remains_terminal_when_cleanup_fails(kanban_home, tmp_path, monkeypatch):
    """An internal error during cleanup never blocks terminal completion.

    ``_cleanup_workspace_on_completion`` must return ``internal_error`` and
    ``complete_task`` must still return True (terminal) even when the
    process-closure helper raises. A scratch workspace is used so the
    patched ``close_workspace_processes`` is reached directly (no dir-gating
    short-circuit).
    """
    ws = tmp_path / "ws"
    ws.mkdir()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(kb, "close_workspace_processes", _boom)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="cleanup-fails", assignee="a")
        conn.execute(
            "UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
            (str(ws), tid),
        )
        conn.commit()
        result = kb._cleanup_workspace_on_completion(conn, tid)
        assert result["outcome"] == "internal_error"
        assert "simulated cleanup failure" in result["error"]

    # Terminal completion must still succeed even when cleanup raises.
    with kb.connect() as conn:
        t2 = kb.create_task(conn, title="terminal-despite-cleanup", assignee="a")
        conn.execute(
            "UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?",
            (str(ws), t2),
        )
        conn.commit()
        kb.claim_task(conn, t2)
        assert kb.complete_task(conn, t2, result="done")
        assert kb.get_task(conn, t2).status == "done"
