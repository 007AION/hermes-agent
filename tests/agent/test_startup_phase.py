"""Safe startup-phase instrumentation: unit, hostile, and RED/GREEN coverage.

The RED case is ``test_startup_path_emits_phase_markers``: before the hooks are
wired into the child-startup path, constructing an agent with tracing enabled
emits *no* markers, proving the exact observability gap. After the hooks land,
the same test passes with the required ordered phase markers.
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
    # Silence any real file handler side effects during the test.
    yield handler
    logger.removeHandler(handler)
    logger.propagate = True


@pytest.fixture
def trace_on(monkeypatch):
    monkeypatch.setenv(sp.TRACE_ENV, "1")
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_disabled_by_default(capture, monkeypatch):
    monkeypatch.delenv(sp.TRACE_ENV, raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    sp.emit("run_agent_import", "ok")
    assert capture.records == []


def test_enabled_via_trace_env(capture, trace_on):
    sp.emit("run_agent_import", "ok")
    assert len(capture.records) == 1
    assert capture.records[0]["phase"] == "run_agent_import"
    assert capture.records[0]["status"] == "ok"


def test_enabled_via_elder_source(capture, monkeypatch):
    monkeypatch.delenv(sp.TRACE_ENV, raising=False)
    monkeypatch.setenv("HERMES_SESSION_SOURCE", sp.ELDER_SESSION_SOURCE)
    sp.emit("process_spawn", "ok")
    assert len(capture.records) == 1
    assert capture.records[0]["phase"] == "process_spawn"


# ---------------------------------------------------------------------------
# Fail-closed vocabulary (hostile)
# ---------------------------------------------------------------------------


def test_unknown_phase_is_dropped(capture, trace_on):
    # A caller that tries to smuggle a free-form string as a phase gets nothing.
    sp.emit("sk-LIVE1234567890abcdef", "ok")
    assert capture.records == []


def test_unknown_status_collapses_to_error(capture, trace_on):
    sp.emit("provider_call", "sk-secret-here")
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
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
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
