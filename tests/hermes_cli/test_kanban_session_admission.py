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
2. **Origin-authenticated TOCTOU backstop** — an active-session admission
   refusal carries a dedicated exit sentinel (``KANBAN_SESSION_LIMIT_EXIT_CODE``)
   *plus* a durable ``session_admission_refused`` receipt binding the exit to an
   actual ``_claim_active_session`` refusal for that exact ``(task_id, pid)``.
   The reap classifier only maps the sentinel to a ``session_capped`` requeue
   (no failure counted) when that receipt is present; an unrelated rc=69 without
   a receipt stays a real nonzero failure that the circuit breaker bounds.
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


def _write_refusal_receipt(kb, conn, task_id, pid, *, run_id=None):
    """Append a durable admission-refusal receipt bound to ``(task_id, pid, run_id)``.

    The nonce must equal the task's ``current_run_id`` (the dispatcher-known
    run identity) for ``detect_crashed_workers`` to authenticate it. When
    ``run_id`` is omitted, read it from the task row so callers that claim the
    task first get a matching receipt for free.
    """
    if run_id is None:
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["current_run_id"]
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            task_id,
            "session_admission_refused",
            {
                "pid": int(pid),
                "session_id": "live-session",
                "profile": "alpha",
                "nonce": str(run_id),
            },
        )


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
    # The raw sentinel is classified as ``session_limit_exit`` — origin
    # authentication (receipt binding) happens in ``detect_crashed_workers``,
    # not here, so an unrelated rc=69 is never silently mapped to session_capped.
    assert kind == "session_limit_exit"
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
    """The origin-authenticated TOCTOU backstop: a session-limit sentinel exit
    *bound to a durable admission-refusal receipt* releases the task to ``ready``
    and leaves ``consecutive_failures`` untouched, so a late lease release can
    never trip the breaker — across many hits."""
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
            # Origin-authenticate: bind the sentinel to an actual admission
            # refusal for this pid before recording the exit.
            _write_refusal_receipt(kb, conn, tid, pid)
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


def test_unrelated_rc69_without_receipt_is_real_failure(
    isolated_session_admission_env, monkeypatch
):
    """Hostile regression: an unrelated rc=69 with NO admission-refusal receipt
    is a real nonzero failure, not a zero-budget session_capped replay."""
    kb = isolated_session_admission_env["kanban_db"]

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect_closing() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="unrelated-69", assignee="alpha")

        for i in range(2):  # DEFAULT_FAILURE_LIMIT == 2
            pid = 90000 + i
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, "
                "claim_lock=? WHERE id=?",
                (pid, f"{host}:w{i}", tid),
            )
            conn.commit()
            # Sentinel exit but NO receipt -> must be a real failure.
            kb._record_worker_exit(
                pid, _exited_status(kb.KANBAN_SESSION_LIMIT_EXIT_CODE)
            )
            crashed = kb.detect_crashed_workers(conn)
            assert tid in crashed, f"hit {i}: unrelated rc=69 must be a crash"
            sc = getattr(kb.detect_crashed_workers, "_last_session_capped", [])
            assert tid not in sc, f"hit {i}: must not be session_capped"

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"unrelated rc=69 must trip the breaker, got {task.status}"
        )


def test_record_session_admission_refusal_writes_verifiable_receipt(
    isolated_session_admission_env,
):
    """The public receipt writer persists a run-bound receipt that the
    dispatcher's verifier can read back (durable + distinguishable)."""
    kb = isolated_session_admission_env["kanban_db"]

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="refused", assignee="alpha")

    pid = 77777
    run_id = "run-abc-123"
    assert (
        kb.record_session_admission_refusal(
            task_id=tid, pid=pid, session_id="s", profile="alpha", nonce=run_id
        )
        is True
    )

    # Read back on a fresh connection to prove durability across processes.
    with kb.connect_closing() as conn:
        # Authenticates only when the dispatcher-known run identity matches.
        assert (
            kb._has_session_admission_refusal_receipt(conn, tid, pid, run_id=run_id)
            is True
        )
        # A different pid is not authenticated.
        assert (
            kb._has_session_admission_refusal_receipt(conn, tid, pid + 1, run_id=run_id)
            is False
        )
        # A stale / mismatched run identity is rejected (non-replayable).
        assert (
            kb._has_session_admission_refusal_receipt(
                conn, tid, pid, run_id="other-run"
            )
            is False
        )
        # A missing run identity is rejected (fail closed).
        assert kb._has_session_admission_refusal_receipt(conn, tid, pid) is False


def test_receipt_without_nonce_does_not_authenticate(
    isolated_session_admission_env,
):
    """Hostile regression: a receipt with no nonce (or a nonce that does not
    match the current run identity) is rejected — it cannot authenticate the
    rc=69 sentinel."""
    kb = isolated_session_admission_env["kanban_db"]

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="no-nonce", assignee="alpha")
        # A receipt carrying only a matching pid, with NO nonce.
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                "session_admission_refused",
                {"pid": 12345},
            )
        # Even with the current run identity supplied, a nonce-less receipt
        # cannot authenticate (its nonce is empty != run_id).
        assert (
            kb._has_session_admission_refusal_receipt(conn, tid, 12345, run_id="42")
            is False
        )
        # And with no run identity at all it is rejected too.
        assert kb._has_session_admission_refusal_receipt(conn, tid, 12345) is False


def test_session_capacity_unreadable_is_observable_and_bounded(
    isolated_session_admission_env, monkeypatch
):
    """Hostile regression: a persistently unreadable profile config/registry is
    surfaced observably (dedicated bucket + event) and stays bounded (fail-open
    to a single spawn, not a silent unbounded replay)."""
    kb = isolated_session_admission_env["kanban_db"]

    monkeypatch.setattr(
        kb,
        "_profile_session_capacity",
        lambda _assignee: kb._SESSION_CAPACITY_UNREADABLE,
    )

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="unreadable-cap", assignee="alpha")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    # Observable: the degraded read is surfaced in a dedicated bucket.
    assert res.skipped_profile_session_capacity_unknown == [(tid, "alpha")]
    # Bounded: it still fail-opens to exactly one spawn (the child's own gate
    # protects correctness), not an unbounded replay.
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == tid
    assert not res.skipped_profile_session_capped

    # A durable observable event was emitted.
    with kb.connect_closing() as conn:
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? "
                "AND kind='session_capacity_unknown'",
                (tid,),
            ).fetchall()
        ]
        assert kinds == ["session_capacity_unknown"]


def test_profile_session_capacity_distinguishes_unreadable(
    isolated_session_admission_env, monkeypatch
):
    """The preflight distinguishes uncapped / non-existent / unreadable: a
    synthetic or uncapped profile fails open (None), while an unreadable config
    surfaces the distinguishable ``_SESSION_CAPACITY_UNREADABLE`` sentinel."""
    kb = isolated_session_admission_env["kanban_db"]

    # Non-existent profile -> fail-open (None), NOT "capacity unknown".
    assert kb._profile_session_capacity("alice") is None

    # Existing uncapped profile (beta has no config) -> None.
    assert kb._profile_session_capacity("beta") is None

    # Existing capped profile with an empty registry -> (active, max).
    assert kb._profile_session_capacity("alpha") == (0, 1)

    # Unreadable config -> the distinguishable sentinel.
    import hermes_cli.config as _cfg

    def _boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(_cfg, "read_raw_config", _boom)
    assert kb._profile_session_capacity("alpha") is kb._SESSION_CAPACITY_UNREADABLE


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


def test_stale_receipt_pid_reuse_cannot_authenticate_later_run(
    isolated_session_admission_env, monkeypatch
):
    """Hostile stale-receipt / PID-reuse regression: one genuine receipt from
    an earlier run cannot authenticate a later run even when the OS recycles
    the pid and no new receipt is written."""
    kb = isolated_session_admission_env["kanban_db"]

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect_closing() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="reuse", assignee="alpha")

        # Run 1: claim + genuine refusal + receipt bound to run 1's identity.
        pid = 54321
        kb.claim_task(conn, tid, claimer=f"{host}:r1")
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        _write_refusal_receipt(kb, conn, tid, pid)  # nonce == run 1 current_run_id
        kb._record_worker_exit(pid, _exited_status(kb.KANBAN_SESSION_LIMIT_EXIT_CODE))
        kb.detect_crashed_workers(conn)  # -> authenticated session_capped (run 1)

        # Run 2: same task re-claimed with a RECYCLED pid, but NO new receipt.
        kb.claim_task(conn, tid, claimer=f"{host}:r2")  # new run, new run id
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        # Unrelated rc=69 (no receipt) on the recycled pid.
        kb._record_worker_exit(pid, _exited_status(kb.KANBAN_SESSION_LIMIT_EXIT_CODE))
        crashed = kb.detect_crashed_workers(conn)

        # The stale run-1 receipt must NOT authenticate run 2 -> real failure.
        assert tid in crashed
        sc = getattr(kb.detect_crashed_workers, "_last_session_capped", [])
        assert tid not in sc


def test_corrupt_registry_produces_capacity_unknown_not_empty(
    isolated_session_admission_env,
):
    """Hostile regression: a corrupt/unreadable active-session registry is
    surfaced as explicit capacity-unknown, never silently collapsed to
    ``active_count == 0`` (which would fail open and spawn)."""
    kb = isolated_session_admission_env["kanban_db"]
    alpha_home = isolated_session_admission_env["alpha_home"]

    registry = Path(alpha_home) / "runtime" / "active_sessions.json"
    registry.write_text("{ this is not valid json !!!", encoding="utf-8")

    # The corrupt registry is NOT treated as empty (0 active): it is explicit
    # capacity-unknown.
    assert kb._profile_session_capacity("alpha") is kb._SESSION_CAPACITY_UNREADABLE


def test_semantically_corrupt_registry_entries_produce_capacity_unknown(
    isolated_session_admission_env,
):
    """Hostile regression: a syntactically VALID registry whose entry members
    are semantically corrupt (non-object members) is surfaced as explicit
    capacity-unknown, never silently filtered down to ``active_count == 0``
    (which would fail open and spawn under unknown real capacity)."""
    kb = isolated_session_admission_env["kanban_db"]
    alpha_home = isolated_session_admission_env["alpha_home"]

    registry = Path(alpha_home) / "runtime" / "active_sessions.json"
    # Syntactically valid JSON with a semantically invalid entry member.
    registry.write_text('{"entries": ["not-an-entry"]}', encoding="utf-8")

    # The semantically-corrupt registry must be explicit capacity-unknown, not
    # silently collapsed to (0, 1).
    assert kb._profile_session_capacity("alpha") is kb._SESSION_CAPACITY_UNREADABLE


def test_persistent_capacity_unknown_plus_authenticated_refusal_is_bounded(
    isolated_session_admission_env, monkeypatch
):
    """Hostile multi-tick regression: persistent capacity-unknown combined with
    an authenticated admission refusal must NOT immediately zero-budget
    respawn-loop. The second (and later) tick defers instead of re-spawning."""
    kb = isolated_session_admission_env["kanban_db"]

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(
        kb,
        "_profile_session_capacity",
        lambda _assignee: kb._SESSION_CAPACITY_UNREADABLE,
    )

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="loop", assignee="alpha")

        # Tick 1: fresh task fail-opens to exactly one spawn.
        res1 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        assert len(res1.spawned) == 1
        assert res1.spawned[0][0] == tid

        # The worker (pid 12345 from _fake_spawn) exits 69 with a valid
        # authenticated receipt bound to the current run.
        pid = 12345
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]
        _write_refusal_receipt(kb, conn, tid, pid, run_id=run_id)
        kb._record_worker_exit(pid, _exited_status(kb.KANBAN_SESSION_LIMIT_EXIT_CODE))
        crashed = kb.detect_crashed_workers(conn)
        assert tid not in crashed  # authenticated -> session_capped, no crash
        sc = getattr(kb.detect_crashed_workers, "_last_session_capped", [])
        assert tid in sc
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0

        # Tick 2: capacity still unknown AND last run was session_capped ->
        # DEFER, no immediate respawn.
        res2 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        assert len(res2.spawned) == 0
        assert res2.skipped_profile_session_capacity_unknown_deferred == [
            (tid, "alpha")
        ]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"  # still ready, not spawned, not blocked

        # Tick 3: persistent unknown -> still deferred (bounded, no loop).
        res3 = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
        assert len(res3.spawned) == 0
        assert res3.skipped_profile_session_capacity_unknown_deferred == [
            (tid, "alpha")
        ]


def test_dispatch_result_has_session_admission_fields():
    """Schema-level invariant: DispatchResult exposes all session-admission
    receipt fields."""
    from hermes_cli.kanban_db import DispatchResult

    r = DispatchResult()
    assert hasattr(r, "skipped_profile_session_capped")
    assert r.skipped_profile_session_capped == []
    assert hasattr(r, "skipped_profile_session_capacity_unknown")
    assert r.skipped_profile_session_capacity_unknown == []
    assert hasattr(r, "skipped_profile_session_capacity_unknown_deferred")
    assert r.skipped_profile_session_capacity_unknown_deferred == []
    assert hasattr(r, "session_capped")
    assert r.session_capped == []
