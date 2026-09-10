"""Safe, bounded startup-phase markers for the Hermes child-startup path.

Purpose (AION governance issue #863): a prior Elder child stalled *after*
environment load and *before* durable-session/provider-client creation, and the
stall did not reproduce under bounded no-provider probes. The existing logs end
at "environment loaded", so the next observation cannot tell which of the
remaining startup phases (``run_agent`` import, ``AIAgent`` construction,
durable session creation, provider-client creation, provider call) hung, nor
whether the owning cgroup was under pressure at that instant.

This module emits one bounded, structured, redacted log record per phase
boundary (under the ``hermes.startup_phase`` logger, which lands in the normal
``agent.log``) plus the contemporaneous owning-cgroup memory counters. It never
records prompts, argv, provider payloads, credentials, tokens, cookies, private
keys, customer data, or connection material — only a closed set of phase names,
statuses, integer timestamps, and integer cgroup counters.

Safety properties
-----------------
* **Opt-in only.** ``enabled()`` is false unless the session is the Elder
  observation path (``HERMES_SESSION_SOURCE == "elder-observation"``) OR the
  ``agent.startup_phase_trace`` behavioral flag is true in ``config.yaml``
  (the canonical home for non-secret feature flags per AGENTS.md). Normal runs
  are byte-for-byte unaffected — no log line, no config read, no file reads.
* **Fully fail-safe.** ``emit`` wraps its *entire* pipeline — gating,
  vocabulary checks, timestamp construction, cgroup reads, JSON serialization,
  and logging — so a failure anywhere in the diagnostic path (including a
  hostile cgroup/timestamp/logging error) can never propagate into the startup
  or provider path it is required only to observe. A failed marker is dropped,
  never raised.
* **Closed vocabulary.** ``emit`` accepts only phase names from ``_PHASES`` and
  statuses from ``_STATUSES``. An unknown phase is dropped; an unknown status
  collapses to ``"error"``. Callers cannot inject a free-form string.
* **Integer-only cgroup snapshot.** ``cgroup_pressure`` reads only the cgroup
  v2 ``memory.current`` / ``memory.high`` / ``memory.events`` counters and
  emits them as integers (or ``null`` on read failure) — no path strings, no
  arbitrary values.
* **Best-effort.** A read, config, or logging failure never propagates; startup
  correctness must not depend on this instrumentation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Dict, Optional

LOGGER_NAME = "hermes.startup_phase"
ELDER_SESSION_SOURCE = "elder-observation"
CONFIG_FLAG_KEY = "startup_phase_trace"

# Closed phase vocabulary — the only names a caller may emit.
_PHASES = frozenset(
    {
        "process_spawn",
        "run_agent_import",
        "aiagent_constructor",
        "durable_session_create",
        "provider_client_create",
        "provider_call",
    }
)

# Closed status vocabulary — the only statuses a caller may emit.
_STATUSES = frozenset({"begin", "end", "ok", "error", "timeout"})

_logger = logging.getLogger(LOGGER_NAME)


def _config_flag_enabled() -> bool:
    """Read the ``agent.startup_phase_trace`` behavioral flag from config.yaml.

    Lazy import keeps this module importable before the full config stack is
    up (e.g. the ``process_spawn`` marker fires at the very top of
    ``hermes_cli/main.py``). Any failure — missing config, import error,
    malformed value — disables the flag and returns ``False``; the Elder
    session-source gate remains the automatic path.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return False
    try:
        node = cfg.get("agent")
    except Exception:
        return False
    if not isinstance(node, dict):
        return False
    try:
        return bool(node.get(CONFIG_FLAG_KEY, False))
    except Exception:
        return False


def enabled() -> bool:
    """True when startup-phase tracing is active for this process.

    Activation is automatic on the Elder observation path
    (``HERMES_SESSION_SOURCE == "elder-observation"``) or via the
    ``agent.startup_phase_trace`` behavioral flag in ``config.yaml``.
    """
    try:
        if os.environ.get("HERMES_SESSION_SOURCE", "").strip() == ELDER_SESSION_SOURCE:
            return True
    except Exception:
        pass
    return _config_flag_enabled()


# ---------------------------------------------------------------------------
# Owning-cgroup pressure (integer counters only)
# ---------------------------------------------------------------------------


def _own_cgroup_path() -> Optional[str]:
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    match = re.search(r"^0::(.+)$", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _read_cgroup_int(name: str) -> Optional[int]:
    path = _own_cgroup_path()
    if not path:
        return None
    try:
        with open(f"/sys/fs/cgroup{path}/{name}", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _read_cgroup_event(events_raw: Optional[str], key: str) -> Optional[int]:
    if not events_raw:
        return None
    for line in events_raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return int(parts[1])
            except (ValueError, TypeError):
                return None
    return None


def cgroup_pressure() -> Dict[str, Optional[int]]:
    """Return the bounded owning-cgroup memory counters (integers only).

    Every value is an integer or ``None`` (unreadable). No path or string is
    emitted, so this snapshot cannot carry a sensitive value.
    """
    events_raw: Optional[str] = None
    path = _own_cgroup_path()
    if path:
        try:
            with open(f"/sys/fs/cgroup{path}/memory.events", encoding="utf-8") as handle:
                events_raw = handle.read()
        except OSError:
            events_raw = None
    return {
        "memory_current": _read_cgroup_int("memory.current"),
        "memory_high": _read_cgroup_int("memory.high"),
        "memory_events_high": _read_cgroup_event(events_raw, "high"),
        "memory_events_max": _read_cgroup_event(events_raw, "max"),
        "memory_oom_kill": _read_cgroup_event(events_raw, "oom_kill"),
    }


# ---------------------------------------------------------------------------
# Marker emission
# ---------------------------------------------------------------------------


def emit(phase: str, status: str) -> None:
    """Emit one bounded, structured, redacted phase marker (no-op if disabled).

    The record carries only ``phase``, ``status``, epoch ``ts``, monotonic
    ``monotonic_ms``, and the five integer cgroup counters. A caller cannot
    smuggle a prompt, argv value, or credential through this interface.

    Fully fail-safe: the *entire* pipeline — gating, vocabulary, timestamp,
    cgroup snapshot, JSON serialization, and logging — is wrapped so a failure
    in the diagnostic path can never propagate into the startup/provider path
    it observes. A failed marker is dropped, never raised.
    """
    try:
        _emit_impl(phase, status)
    except Exception:
        pass  # instrumentation must never break startup or the observed path


def _emit_impl(phase: str, status: str) -> None:
    if not enabled():
        return
    if phase not in _PHASES:
        return  # fail closed: an unknown phase is never emitted
    if status not in _STATUSES:
        status = "error"  # fail closed: an unknown status collapses to "error"

    record: Dict[str, object] = {
        "phase": phase,
        "status": status,
        "ts": time.time(),
        "monotonic_ms": int(time.monotonic() * 1000),
    }
    record.update(cgroup_pressure())

    _logger.info("startup_phase %s", json.dumps(record, sort_keys=True))


@contextmanager
def phase(name: str):
    """Context manager emitting ``begin`` on entry and ``end``/``error`` on exit.

    Exception and timeout paths still produce a safe last-boundary marker
    (``error``), so a crash inside a phase is attributed to that phase rather
    than leaving the trace silent. Because ``emit`` is fully fail-safe, a
    diagnostic failure can never prevent the wrapped body from running.
    """
    emit(name, "begin")
    try:
        yield
    except BaseException:
        emit(name, "error")
        raise
    else:
        emit(name, "end")
