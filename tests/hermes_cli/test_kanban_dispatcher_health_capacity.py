"""Regression tests for AION-RL2-CORE-01-R34 — capacity-aware dispatcher
health telemetry.

Root cause: ``has_spawnable_ready()`` (shared by the gateway- and CLI-embedded
dispatchers' health telemetry) treated a ready task whose assignee profile is
saturated (``max_concurrent_sessions`` reached, or the per-profile in-flight
cap reached) as "spawnable", so the health probe fired the false "dispatcher
stuck" warning on an entirely capacity-accounted queue.

The fix makes ``has_spawnable_ready`` exclude *known* capacity-deferred tasks
(``skipped_profile_session_capped`` / ``skipped_per_profile_capped``) while
keeping unreadable/unknown capacity fail-closed (still spawnable) so a real
stall is never hidden behind an unknown-capacity deferral.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_health_env(monkeypatch):
    """Fresh HERMES_HOME + kanban home with a capped 'alpha' and uncapped 'beta'."""
    test_home = tempfile.mkdtemp(prefix="kanban_health_capacity_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    alpha_home = os.path.join(test_home, "profiles", "alpha")
    os.makedirs(os.path.join(alpha_home, "runtime"), exist_ok=True)
    # alpha is capped at 1 concurrent active session.
    with open(os.path.join(alpha_home, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write("max_concurrent_sessions: 1\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    monkeypatch.setenv("HERMES_KANBAN_HOME", test_home)
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


# ---------------------------------------------------------------------------
# Unit tests: has_spawnable_ready capacity-awareness
# ---------------------------------------------------------------------------


def test_has_spawnable_ready_false_when_only_session_capped_task(
    isolated_health_env, monkeypatch
):
    """A capped profile holding a live lease is expected deferred work, not a
    spawnable stall: ``has_spawnable_ready`` returns False."""
    kb = isolated_health_env["kanban_db"]
    active_sessions = isolated_health_env["active_sessions"]
    alpha_home = isolated_health_env["alpha_home"]

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: int(pid) == 424242)
    _write_lease(active_sessions, alpha_home, 424242)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="admit-me", assignee="alpha")

    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn) is False


def test_has_spawnable_ready_true_when_mixed_session_capped_and_uncapped(
    isolated_health_env, monkeypatch
):
    """Mixed queue stays fail-closed: one session-capped item must not hide a
    genuinely spawnable (uncapped) item."""
    kb = isolated_health_env["kanban_db"]
    active_sessions = isolated_health_env["active_sessions"]
    alpha_home = isolated_health_env["alpha_home"]

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: int(pid) == 424242)
    _write_lease(active_sessions, alpha_home, 424242)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="alpha-capped", assignee="alpha")
        kb.create_task(conn, title="beta-ok", assignee="beta")

    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn) is True


def test_has_spawnable_ready_true_when_session_capacity_unknown(
    isolated_health_env, monkeypatch
):
    """Unreadable/unknown capacity is fail-closed: still treated as spawnable
    so a real stall is never hidden behind an unknown-capacity deferral."""
    kb = isolated_health_env["kanban_db"]

    monkeypatch.setattr(
        kb,
        "_profile_session_capacity",
        lambda assignee: kb._SESSION_CAPACITY_UNREADABLE,
    )

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="unknown-cap", assignee="alpha")

    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn) is True


def test_has_spawnable_ready_false_when_per_profile_capped(isolated_health_env):
    """A profile already at its in-flight cap is expected deferred work when the
    caller passes ``max_in_progress_per_profile``."""
    kb = isolated_health_env["kanban_db"]

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running = kb.create_task(conn, title="running alpha", assignee="alpha")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'test:1' "
                "WHERE id = ?",
                (running,),
            )
        kb.create_task(conn, title="alpha-ready", assignee="alpha")

    with kb.connect_closing() as conn:
        assert (
            kb.has_spawnable_ready(conn, max_in_progress_per_profile=1) is False
        )


def test_has_spawnable_ready_true_when_per_profile_cap_not_saturated(
    isolated_health_env,
):
    """Below the per-profile cap the task is still spawnable."""
    kb = isolated_health_env["kanban_db"]

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="alpha-ready", assignee="alpha")

    with kb.connect_closing() as conn:
        assert kb.has_spawnable_ready(conn, max_in_progress_per_profile=1) is True


# ---------------------------------------------------------------------------
# Gateway health-telemetry loop tests (RED -> GREEN)
# ---------------------------------------------------------------------------


def _run_watcher_for_ticks(isolated_health_env, monkeypatch, caplog, config_cap=None):
    """Drive ``GatewayRunner._kanban_dispatcher_watcher`` for >= HEALTH_WINDOW
    ticks and return the captured log messages.

    ``dispatch_once`` is stubbed to return an empty result (no subprocess
    spawns, no skip buckets); the health probe's ``has_spawnable_ready`` runs
    for real against the isolated board, so a session-capped queue reads as
    "not spawnable" while an uncapped ready task reads as "spawnable".
    """
    from gateway.run import GatewayRunner
    import hermes_cli.config as _cfg_mod
    import hermes_cli.kanban_db as _kb

    cfg = {
        "kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "auto_decompose": False,
        }
    }
    if config_cap is not None:
        cfg["kanban"]["max_in_progress_per_profile"] = config_cap
    monkeypatch.setattr(_cfg_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(_kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(
        _kb,
        "list_boards",
        lambda include_archived=False: [{"slug": _kb.DEFAULT_BOARD}],
    )
    monkeypatch.setattr(_kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(_kb, "dispatch_once", lambda *a, **k: _kb.DispatchResult())

    runner = object.__new__(GatewayRunner)
    runner._running = True

    calls = {"to_thread": 0}

    async def _to_thread(fn, *args, **kwargs):
        calls["to_thread"] += 1
        result = fn(*args, **kwargs)
        # 3 to_thread calls per tick (reap + _tick_once + _ready_nonempty).
        # Stop after 7 ticks so HEALTH_WINDOW (6) is definitively reached.
        if calls["to_thread"] >= 21:
            runner._running = False
        return result

    async def _sleep(_delay):
        return None

    monkeypatch.setattr("gateway.run.asyncio.to_thread", _to_thread)
    monkeypatch.setattr("gateway.run.asyncio.sleep", _sleep)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        asyncio.run(
            asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=10.0)
        )

    return [record.getMessage() for record in caplog.records]


def test_gateway_no_stuck_warning_when_session_capped(
    isolated_health_env, monkeypatch, caplog
):
    """An entirely session-cap-deferred queue must NOT advance bad_ticks or emit
    the "dispatcher stuck" warning across HEALTH_WINDOW ticks."""
    kb = isolated_health_env["kanban_db"]
    active_sessions = isolated_health_env["active_sessions"]
    alpha_home = isolated_health_env["alpha_home"]

    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: int(pid) == 424242)
    _write_lease(active_sessions, alpha_home, 424242)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="admit-me", assignee="alpha")

    messages = _run_watcher_for_ticks(isolated_health_env, monkeypatch, caplog)
    assert not any("dispatcher stuck" in m for m in messages)


def test_gateway_still_warns_when_spawnable_uncapped(
    isolated_health_env, monkeypatch, caplog
):
    """A genuinely spawnable (uncapped) ready task with zero spawns still trips
    the bounded health warning — the fail-closed path is preserved."""
    kb = isolated_health_env["kanban_db"]

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="beta-stuck", assignee="beta")

    messages = _run_watcher_for_ticks(isolated_health_env, monkeypatch, caplog)
    assert any("dispatcher stuck" in m for m in messages)


def test_gateway_forwards_per_profile_cap(isolated_health_env, monkeypatch, caplog):
    """The gateway health probe forwards ``max_in_progress_per_profile`` to
    ``has_spawnable_ready`` so the per-profile-cap deferral is also excluded."""
    from hermes_cli import kanban_db as _kb

    seen = {}

    def _spy_has_spawnable_ready(conn, *, max_in_progress_per_profile=None):
        seen["max_in_progress_per_profile"] = max_in_progress_per_profile
        return False

    monkeypatch.setattr(_kb, "has_spawnable_ready", _spy_has_spawnable_ready)

    _run_watcher_for_ticks(isolated_health_env, monkeypatch, caplog, config_cap=2)

    assert seen.get("max_in_progress_per_profile") == 2
