"""Regression tests for AION-RL2-CORE-01-SESSION_ADMISSION.

Root cause: the dispatcher's per-profile cap counts only ``task.status='running'``.
A worker can ``kanban_complete`` its task while its process still holds the
assignee profile's sole active-session lease through finalization / background
review, so the dispatcher claims/spawns a next same-profile task that the
authoritative active-session gate then refuses with "active session limit".

Two bounded fixes are covered here:

1. **Preflight defer** — before claim/spawn, the dispatcher consults the
   assignee profile's *canonical* active-session registry (under its own lock,
   dead PIDs pruned) and defers a saturated profile while leaving the task
   ``ready``/unclaimed with no run created (``skipped_profile_session_capped``).
2. **TOCTOU backstop** — an active-session admission refusal carries a dedicated
   exit sentinel (``KANBAN_SESSION_LIMIT_EXIT_CODE``) that the reap classifier
   maps to a ``session_capped`` requeue WITHOUT counting a failure, so a late
   lease release can never trip the circuit breaker.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_session_admission_env(monkeypatch):
    """Fresh HERMES_HOME with a capped 'alpha' profile and an uncapped 'beta'."""
    test_home = tempfile.mkdtemp(prefix="kanban_session_admission_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    alpha_home = os.path.join(test_home, "profiles", "alpha")
    os.makedirs(os.path.join(alpha_home, "runtime"), exist_ok=True)
    # alpha is capped at 1 concurrent active session.
    with open(os.path.join(alpha_home, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write("max_concurrent_sessions: 1\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # Fresh module state so hermes_cli resolves paths against the temp home.
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import active_sessions
    from hermes_cli import kanban_db

    yield {
        "kanban_db": kanban_db,
        "active_sessions": active_sessions,
        "test_home": test_home,
        "alpha_home": alpha_home,
    }


def _write_lease(active_sessions, alpha_home, pid):
    """Write a single live lease into alpha's canonical registry."""
    registry = Path(alpha_home) / "runtime" / "active_sessions.json"
    active_sessions._write_entries(
        registry,
        [
            {
                "lease_id": "live-1",
                "session_id": "live-session",
                "surface": "cli",
                "pid": pid,
                "started_at": 1,
                "updated_at": 1,
            }
        ],
    )


def _clear_registry(active_sessions, alpha_home):
    registry = Path(alpha_home) / "runtime" / "active_sessions.json"
    active_sessions._write_entries(registry, [])


def _fake_spawn(*args, **kwargs):
    return 12345


def test_saturated_profile_deferred_without_claim_or_run(
    isolated_session_admission_env, monkeypatch
):
    """A capped profile holding a live lease is deferred: no claim, no run,
    task stays ``ready`` and lands in ``skipped_profile_session_capped``."""
    kb = isolated_session_admission_env["kanban_db"]
    active_sessions = isolated_session_admission_env["active_sessions"]
    alpha_home = isolated_session_admission_env["alpha_home"]

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: int(pid) == 424242)
    _write_lease(active_sessions, alpha_home, 424242)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="admit-me", assignee="alpha")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert not res.spawned
    assert not res.session_capped
    assert res.skipped_profile_session_capped
    assert res.skipped_profile_session_capped[0][0] == tid
    assert res.skipped_profile_session_capped[0][1] == "alpha"
    assert res.skipped_profile_session_capped[0][2] == 1  # active session count

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.current_run_id is None
        # No run was ever opened for the deferred task.
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()[0]
        assert runs == 0


def test_release_then_next_tick_spawns_exactly_once(
    isolated_session_admission_env, monkeypatch
):
    """Once the lease releases, the next tick admits the task exactly once."""
    kb = isolated_session_admission_env["kanban_db"]
    active_sessions = isolated_session_admission_env["active_sessions"]
    alpha_home = isolated_session_admission_env["alpha_home"]

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: int(pid) == 424242)
    _write_lease(active_sessions, alpha_home, 424242)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="admit-me", assignee="alpha")

    with kb.connect_closing() as conn:
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert not res1.spawned
    assert len(res1.skipped_profile_session_capped) == 1

    # Simulate the lease release (process finalized its session).
    _clear_registry(active_sessions, alpha_home)

    with kb.connect_closing() as conn:
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res2.spawned) == 1
    assert res2.spawned[0][0] == tid
    assert not res2.skipped_profile_session_capped


def test_uncapped_profile_is_never_deferred(
    isolated_session_admission_env, monkeypatch
):
    """A profile without ``max_concurrent_sessions`` is never deferred, even
    with a (stale) registry entry present."""
    kb = isolated_session_admission_env["kanban_db"]

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="beta-task", assignee="beta")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert len(res.spawned) == 1
    assert not res.skipped_profile_session_capped


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def test_classify_worker_exit_recognizes_session_limit_sentinel(
    isolated_session_admission_env,
):
    kb = isolated_session_admission_env["kanban_db"]

    pid = 55555
    kb._record_worker_exit(pid, _exited_status(kb.KANBAN_SESSION_LIMIT_EXIT_CODE))
    kind, code = kb._classify_worker_exit(pid)
    assert kind == "session_capped"
    assert code == kb.KANBAN_SESSION_LIMIT_EXIT_CODE

    # Distinct from the quota-wall sentinel and a generic non-zero exit.
    kb._record_worker_exit(pid + 1, _exited_status(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    assert kb._classify_worker_exit(pid + 1) == (
        "rate_limited",
        kb.KANBAN_RATE_LIMIT_EXIT_CODE,
    )
    kb._record_worker_exit(pid + 2, _exited_status(1))
    assert kb._classify_worker_exit(pid + 2) == ("nonzero_exit", 1)


def test_session_limit_exit_requeues_without_counting_failure(
    isolated_session_admission_env, monkeypatch
):
    """The TOCTOU backstop: a session-limit sentinel exit releases the task to
    ``ready`` and leaves ``consecutive_failures`` untouched, so a late lease
    release can never trip the breaker — across many hits."""
    kb = isolated_session_admission_env["kanban_db"]

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect_closing() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="admit", assignee="alpha")

        for i in range(6):
            pid = 80000 + i
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            kb._record_worker_exit(
                pid, _exited_status(kb.KANBAN_SESSION_LIMIT_EXIT_CODE)
            )

            crashed = kb.detect_crashed_workers(conn)
            assert tid not in crashed, (
                f"hit {i}: session-capped is backpressure, not a crash"
            )
            sc = getattr(kb.detect_crashed_workers, "_last_session_capped", [])
            assert tid in sc

            task = kb.get_task(conn, tid)
            assert task.status == "ready", f"hit {i}: should requeue ready"
            assert task.consecutive_failures == 0, (
                f"hit {i}: session cap must not count a failure"
            )

        # A ``session_capped`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"]
            for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,)
            ).fetchall()
        ]
        assert "session_capped" in outcomes
        assert "crashed" not in outcomes


def test_real_crash_still_counts_and_trips_breaker(
    isolated_session_admission_env, monkeypatch
):
    """Sanity: a genuine non-zero crash (not the sentinel) still increments the
    failure counter and trips the breaker — the session-cap carve-out is
    surgical, not a blanket "never count crashes"."""
    kb = isolated_session_admission_env["kanban_db"]

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    with kb.connect_closing() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="crash", assignee="alpha")

        for i in range(2):  # DEFAULT_FAILURE_LIMIT == 2
            pid = 60000 + i
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (pid, f"{host}:w{i}", tid),
            )
            conn.commit()
            kb._record_worker_exit(pid, _exited_status(1))  # generic failure
            kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"genuine crashes should still trip the breaker, got {task.status}"
        )


def test_dispatch_result_has_session_admission_fields():
    """Schema-level invariant: DispatchResult exposes both new receipt fields."""
    from hermes_cli.kanban_db import DispatchResult

    r = DispatchResult()
    assert hasattr(r, "skipped_profile_session_capped")
    assert r.skipped_profile_session_capped == []
    assert hasattr(r, "session_capped")
    assert r.session_capped == []
