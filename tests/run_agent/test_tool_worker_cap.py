"""Bound on concurrent tool worker fan-out (shared-infra capacity control).

``agent.tool_executor._MAX_TOOL_WORKERS`` caps how many tool calls the
concurrent executor schedules at once.  On single-cgroup shared
infrastructure, the historical 8-way fan-out (8 worker threads, each able to
spawn terminal/tirith/search subprocesses or provider threads) plus the
gateway's own threads can transiently exceed the systemd ``TasksMax`` and
drive ``pids.events.max`` denials that surface as ``[Errno 11] Resource
temporarily unavailable`` at worker/subprocess spawn.

The safe default is at most 4 concurrent tool workers.  These tests pin that
safety bound and prove a batch of 8+ safe tool calls never exceeds it while
preserving model result ordering and the existing dispatch guards.
"""

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from agent.tool_executor import _MAX_TOOL_WORKERS


def _tc(name="web_search", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{name}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _make_tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture()
def agent():
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("web_search", "terminal"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def test_worker_cap_default_is_at_most_four():
    """The safe default must not exceed 4 concurrent tool workers.

    A higher default re-opens the shared-cgroup ``pids.max`` fan-out risk
    this bound exists to contain.  (Lower is safer and allowed.)
    """
    assert _MAX_TOOL_WORKERS <= 4


def test_run_agent_mirror_stays_in_sync_with_executor_cap():
    """The duplicated cap in ``run_agent`` must mirror the live executor cap.

    ``agent.tool_executor._MAX_TOOL_WORKERS`` is the live value consumed by
    the concurrent executor; ``run_agent._MAX_TOOL_WORKERS`` is its exported
    mirror.  Drift between the two would leave readers/tests consulting a
    stale cap.
    """
    import run_agent

    assert run_agent._MAX_TOOL_WORKERS == _MAX_TOOL_WORKERS


def test_batch_of_eight_never_exceeds_cap_and_preserves_order(agent):
    """A batch of 8 parallel-safe tools must never run more than the cap
    concurrently, and results must land in emission order."""
    active = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def fake_invoke(function_name, function_args, effective_task_id, *args, **kwargs):
        with lock:
            active["n"] += 1
            active["peak"] = max(active["peak"], active["n"])
        # Hold the "tool" open long enough to overlap with siblings so the
        # peak measurement reflects genuine concurrent fan-out.
        time.sleep(0.05)
        with lock:
            active["n"] -= 1
        return json.dumps({"ok": function_name})

    agent._invoke_tool = MagicMock(side_effect=fake_invoke)

    calls = [
        _tc("web_search", '{"query":"q"}', call_id=f"c{i}") for i in range(8)
    ]
    msg = SimpleNamespace(content="", tool_calls=calls)
    messages = []

    agent._execute_tool_calls(msg, messages, "task-1")

    # One result per call, in the model's emission order.
    assert [m["tool_call_id"] for m in messages] == [f"c{i}" for i in range(8)]
    assert all(m["role"] == "tool" for m in messages)

    # Never more than the cap (and hence at most 4) active at once.
    assert active["peak"] <= _MAX_TOOL_WORKERS
    assert active["peak"] <= 4
