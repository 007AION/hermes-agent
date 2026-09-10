"""Safe startup-phase instrumentation: unit, hostile, and RED/GREEN coverage.

The RED case is ``test_startup_path_emits_phase_markers``: before the hooks are
wired into the child-startup path, constructing an agent with tracing enabled
emits *no* markers, proving the exact observability gap. After the hooks land,
the same test passes with the required ordered phase markers.

Two audit blockers are covered explicitly:

* ``STARTUP_TRACE_EMIT_CAN_PROPAGATE`` — the *entire* emit pipeline (gating,
  timestamp construction, cgroup snapshot, JSON, logging) must be fail-safe so
  a diagnostic failure can never prevent the wrapped provider dispatch or
  durable-session creation. Covered by the ``*_survives_*`` hostile tests that
  drive the real hooks with a cgroup/timestamp/logging failure injected.
* ``NON_SECRET_BEHAVIOR_ENV_FLAG`` — the opt-in flag must live in
  ``config.yaml`` (``agent.startup_phase_trace``), not a user-facing
  ``HERMES_*`` env var. Covered by ``test_env_var_no_longer_gates``,
  ``test_config_flag_reads_config_yaml``, and
  ``test_default_config_has_startup_phase_trace``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent import startup_phase as sp

LOGGER_NAME = "hermes.startup_phase"
REQUIRED_PHASES = [
    "process_spawn",
    "run_agent_import",
    "aiagent_constructor",
    "durable_session_create",
    "provider_client_create",
    "provider_call",
]

ALLOWED_KEYS = {
    "phase",
    "status",
    "ts",
    "monotonic_ms",
    "memory_current",
    "memory_high",
    "memory_events_high",
    "memory_events_max",
    "memory_oom_kill",
}


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            message = record.getMessage()
            if message.startswith("startup_phase "):
                self.records.append(json.loads(message[len("startup_phase ") :]))
        except Exception:
            pass


@pytest.fixture
def capture(monkeypatch):
    handler = _CaptureHandler()
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield handler
    logger.removeHandler(handler)
    logger.propagate = True


def _force_config_flag_off(monkeypatch):
    # Pin the config.yaml path off so only the intended enabler is active.
    monkeypatch.setattr(sp, "_config_flag_enabled", lambda: False)


@pytest.fixture
def trace_on(monkeypatch):
    """Enable tracing via the Elder observation session source (automatic gate)."""
    _force_config_flag_off(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", sp.ELDER_SESSION_SOURCE)


@pytest.fixture
def config_on(monkeypatch):
    """Enable tracing via the config.yaml behavioral flag."""
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.setattr(sp, "_config_flag_enabled", lambda: True)


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    _force_config_flag_off(monkeypatch)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_disabled_by_default(capture, disabled):
    sp.emit("run_agent_import", "ok")
    assert capture.records == []


def test_enabled_via_session_source(capture, trace_on):
    sp.emit("run_agent_import", "ok")
    assert len(capture.records) == 1
    assert capture.records[0]["phase"] == "run_agent_import"
    assert capture.records[0]["status"] == "ok"


def test_enabled_via_elder_source(capture, monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", sp.ELDER_SESSION_SOURCE)
    sp.emit("process_spawn", "ok")
    assert len(capture.records) == 1
    assert capture.records[0]["phase"] == "process_spawn"


def test_enabled_via_config_flag(capture, config_on):
    sp.emit("run_agent_import", "ok")
    assert len(capture.records) == 1


def test_env_var_no_longer_gates(capture, monkeypatch):
    # The old user-facing env-only toggle must have no effect. A non-secret
    # behavioral flag lives in config.yaml, not a HERMES_* env var.
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    _force_config_flag_off(monkeypatch)
    monkeypatch.setenv("HERMES_STARTUP_PHASE_TRACE", "1")
    sp.emit("run_agent_import", "ok")
    assert capture.records == []


def test_config_flag_reads_config_yaml(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: {"agent": {"startup_phase_trace": True}},
    )
    assert sp._config_flag_enabled() is True

    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: {"agent": {"startup_phase_trace": False}},
    )
    assert sp._config_flag_enabled() is False

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {})
    assert sp._config_flag_enabled() is False


def test_config_flag_returns_false_on_loader_failure(monkeypatch):
    from hermes_cli import config as config_mod

    def _boom():
        raise RuntimeError("config stack down")

    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    assert sp._config_flag_enabled() is False


def test_default_config_has_startup_phase_trace():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["agent"]["startup_phase_trace"] is False


# ---------------------------------------------------------------------------
# Fail-closed vocabulary (hostile)
# ---------------------------------------------------------------------------


def test_unknown_phase_is_dropped(capture, trace_on):
    # A caller that tries to smuggle a free-form string as a phase gets nothing.
    sp.emit("sk-LIV...cdef", "ok")
    assert capture.records == []


def test_unknown_status_collapses_to_error(capture, trace_on):
    sp.emit("provider_call", "***")
    assert len(capture.records) == 1
    assert capture.records[0]["phase"] == "provider_call"
    assert capture.records[0]["status"] == "error"


# ---------------------------------------------------------------------------
# Bounded, redacted record shape (hostile)
# ---------------------------------------------------------------------------


def test_record_only_whitelisted_keys(capture, trace_on):
    sp.emit("provider_call", "begin")
    assert len(capture.records) == 1
    assert set(capture.records[0].keys()) <= ALLOWED_KEYS


def test_no_sensitive_value_enters_record(capture, trace_on, monkeypatch):
    # A credential-shaped value present in the process environment must never
    # appear in the emitted record — emit() has no free-string channel, and the
    # cgroup snapshot is integer-only.
    secret = "sk-pro...7890"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    sp.emit("provider_client_create", "begin")
    assert len(capture.records) == 1
    assert secret not in json.dumps(capture.records[0])


def test_cgroup_fields_are_integer_or_none(capture, trace_on):
    sp.emit("durable_session_create", "begin")
    assert len(capture.records) == 1
    for key in (
        "memory_current",
        "memory_high",
        "memory_events_high",
        "memory_events_max",
        "memory_oom_kill",
    ):
        assert key in capture.records[0]
        assert capture.records[0][key] is None or isinstance(
            capture.records[0][key], int
        )


# ---------------------------------------------------------------------------
# Context manager: exception/timeout last-boundary marker (hostile)
# ---------------------------------------------------------------------------


def test_phase_context_manager_success(capture, trace_on):
    with sp.phase("provider_call"):
        pass
    statuses = [r["status"] for r in capture.records]
    assert statuses == ["begin", "end"]


def test_phase_context_manager_error(capture, trace_on):
    with pytest.raises(RuntimeError):
        with sp.phase("provider_client_create"):
            raise RuntimeError("boom")
    statuses = [r["status"] for r in capture.records]
    assert statuses == ["begin", "error"]


# ---------------------------------------------------------------------------
# Fail-safe emit: diagnostic failures never propagate (STARTUP_TRACE_EMIT_CAN_PROPAGATE)
# ---------------------------------------------------------------------------


def test_emit_swallows_cgroup_failure(capture, trace_on, monkeypatch):
    def _boom():
        raise RuntimeError("cgroup read exploded")

    monkeypatch.setattr(sp, "cgroup_pressure", _boom)
    # Must not raise; a failed diagnostic marker is dropped, never propagated.
    sp.emit("provider_call", "begin")


def test_emit_swallows_timestamp_failure(capture, trace_on, monkeypatch):
    def _boom():
        raise RuntimeError("clock exploded")

    monkeypatch.setattr(sp.time, "time", _boom)
    # Must not raise.
    sp.emit("provider_call", "begin")


def test_emit_swallows_logging_failure(capture, trace_on, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("logger exploded")

    monkeypatch.setattr(sp._logger, "info", _boom)
    # Must not raise.
    sp.emit("provider_call", "begin")


def test_provider_dispatch_survives_instrumentation_failure(capture, trace_on, monkeypatch):
    # Hostile actual-hook probe: the cgroup snapshot pipeline blows up mid-marker.
    # The diagnostic failure must be swallowed and the wrapped provider call must
    # still happen (provider_called=true), not propagate.
    from unittest.mock import MagicMock

    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request

    def _boom():
        raise RuntimeError("cgroup read exploded")

    monkeypatch.setattr(sp, "cgroup_pressure", _boom)

    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "moa"
    agent.client.chat.completions.create.return_value = {"ok": True}

    result = _dispatch_nonstreaming_api_request(
        agent, {}, make_client=lambda *a, **k: None
    )

    assert result == {"ok": True}  # provider was called and returned
    agent.client.chat.completions.create.assert_called_once()


def test_provider_dispatch_survives_timestamp_failure(capture, trace_on, monkeypatch):
    from unittest.mock import MagicMock

    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request

    def _boom():
        raise RuntimeError("clock exploded")

    monkeypatch.setattr(sp.time, "time", _boom)

    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "moa"
    agent.client.chat.completions.create.return_value = {"ok": True}

    result = _dispatch_nonstreaming_api_request(
        agent, {}, make_client=lambda *a, **k: None
    )

    assert result == {"ok": True}
    agent.client.chat.completions.create.assert_called_once()


def test_durable_session_survives_instrumentation_failure(
    capture, trace_on, monkeypatch, tmp_path
):
    def _boom():
        raise RuntimeError("cgroup read exploded")

    monkeypatch.setattr(sp, "cgroup_pressure", _boom)

    agent, db = _build_agent(monkeypatch, tmp_path)
    # Must not raise; the diagnostic failure is swallowed.
    agent._ensure_db_session()
    try:
        agent.close()
    except Exception:
        pass
    try:
        db.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Bounded volume + ordering (GREEN)
# ---------------------------------------------------------------------------


def test_bounded_volume(capture, trace_on):
    for _ in range(50):
        sp.emit("provider_call", "begin")
    # One record per emit — no amplification, no unbounded growth.
    assert len(capture.records) == 50


def test_full_phase_ordering(capture, trace_on):
    for name in REQUIRED_PHASES:
        with sp.phase(name):
            pass
    emitted = [r["phase"] for r in capture.records]
    assert emitted == [p for name in REQUIRED_PHASES for p in (name, name)]


def test_provider_call_hook_emits(capture, trace_on):
    # Exercise the real non-streaming dispatch funnel with a mocked MoA client
    # so the provider_call begin/end markers are observed on the actual path.
    from unittest.mock import MagicMock

    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request

    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "moa"
    agent.client.chat.completions.create.return_value = {"ok": True}

    _dispatch_nonstreaming_api_request(agent, {}, make_client=lambda *a, **k: None)

    assert [r["phase"] for r in capture.records] == ["provider_call", "provider_call"]
    assert [r["status"] for r in capture.records] == ["begin", "end"]


# ---------------------------------------------------------------------------
# RED/GREEN: the real child-startup path emits markers
# ---------------------------------------------------------------------------


def _build_agent(monkeypatch, tmp_path: Path):
    from hermes_state import SessionDB

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    db = SessionDB(db_path=tmp_path / "state.db")

    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        platform="telegram",
        quiet_mode=True,
        session_db=db,
        session_id="test-session",
        skip_context_files=True,
        skip_memory=True,
    )
    return agent, db


def test_startup_path_emits_phase_markers(capture, trace_on, monkeypatch, tmp_path):
    agent, db = _build_agent(monkeypatch, tmp_path)
    # Durable session creation happens on first use.
    agent._ensure_db_session()
    try:
        agent.close()
    except Exception:
        pass
    try:
        db.close()
    except Exception:
        pass

    phases = [r["phase"] for r in capture.records]
    # The three construction phases must be observable and ordered.
    assert "run_agent_import" in phases
    assert "aiagent_constructor" in phases
    assert "provider_client_create" in phases
    assert "durable_session_create" in phases

    # Ordered: import -> constructor -> client -> session (session happens
    # after construction; provider client is built during construction).
    assert phases.index("run_agent_import") < phases.index("aiagent_constructor")
    assert phases.index("provider_client_create") < phases.index(
        "durable_session_create"
    )


def test_disabled_constructs_agent_with_real_config(capture, monkeypatch, tmp_path):
    # Normal (disabled) run: no session source, and the *real* config loader
    # must be consulted (agent.startup_phase_trace absent -> default False)
    # without raising, and the agent must construct with zero markers emitted.
    # This pins the "normal runs are byte-for-byte unaffected" acceptance claim.
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    # Deliberately do NOT monkeypatch _config_flag_enabled — exercise the real
    # load_config_readonly() path during construction.
    agent, db = _build_agent(monkeypatch, tmp_path)
    agent._ensure_db_session()
    try:
        agent.close()
    except Exception:
        pass
    try:
        db.close()
    except Exception:
        pass
    assert capture.records == []
