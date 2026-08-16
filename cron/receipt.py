"""Immutable admission-to-release receipt capture for cron executions.

Pure, side-effect-free helpers used by :mod:`cron.executions` to record one
identity-bound, audit-reproducible receipt per natural agent-backed execution.
Nothing here writes to disk or a database; it only *observes* the live
machine (cgroup counters, ``/proc`` process state, the selected job and its
sibling jobs) and returns structured, JSON-serialisable snapshots.

Fail-closed contract
--------------------
Every probe is best-effort *read* only. A field that cannot be resolved —
missing cgroup, unreadable ``/proc``, absent job, ambiguous identity — is
recorded as ``None`` together with a human-readable entry in ``capture_errors``
rather than being silently omitted or synthesised. Consumers treat a non-empty
``capture_errors`` list (or a missing required identity field) as a
fail-closed receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_time import now as _hermes_now

SNAPSHOT_SCHEMA_VERSION = "receipt-snapshot-v1"
HASH_SCHEMA_VERSION = "receipt-hash-v2"
FIELD_DIFF_SCHEMA_VERSION = "receipt-field-diff-v1"

# Field classes used by the classified canonical view and the field-diff
# manifest. Every job field is exactly one of:
#   * ``runtime_claim`` — per-run / claim / volatile: excluded from the stable
#     hash, legitimately moves on every fire.
#   * ``redacted``      — prompt/secret: replaced by a one-way SHA-256 digest.
#   * ``stable``        — definitional: kept in the hash in full.
_FIELD_CLASS_STABLE = "stable"
_FIELD_CLASS_REDACTED = "redacted"
_FIELD_CLASS_RUNTIME_CLAIM = "runtime_claim"

# Job fields whose plaintext must never enter a receipt's canonical
# serialization. They are replaced by a one-way SHA-256 digest of their value
# so drift in the field is still detected without ever persisting (or even
# holding in the canonical string) prompt/config/secret content.
_REDACT_FIELDS = frozenset({
    "prompt", "origin", "deliver", "base_url",
})

# Job fields that legitimately change on every fire (runtime counters and
# transient status). Excluded entirely from the integrity hash: they are not
# part of a job's stable definition, so their normal per-run movement must not
# be misread as drift.
#
# ``execution_id`` and ``session_id`` are runtime claim fields injected by the
# scheduler around a single dispatch: ``execution_id`` is the execution
# ledger's own back-reference to the attempt being written, and ``session_id``
# is the natural cron session identity. Both are absent from the stored job
# record and appear only on the in-memory dict threaded through one run, so
# they MUST be excluded — otherwise the pre-admission and post-release hashes
# of the *same* stable job definition diverge (the R24
# UNRECONCILED_SELECTED_JOB_HASH_DRIFT finding).
_VOLATILE_FIELDS = frozenset({
    "next_run_at", "last_run_at", "last_status", "last_error",
    "last_delivery_error", "repeat", "state", "paused_at", "paused_reason",
    "created_at", "updated_at",
    "execution_id", "session_id",
})


def _sha256_text(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _json_dumps_canonical(value: Any) -> str:
    """Canonical JSON (sorted keys, no whitespace) for stable hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _redacted_value_hash(value: Any) -> str:
    """Hash a redacted (prompt/secret) field so drift is detected, value hidden."""
    return _sha256_text(_json_dumps_canonical(value))


def classify_job_field(key: str) -> str:
    """Classify a job field as ``stable`` / ``redacted`` / ``runtime_claim``.

    This is the single source of truth for what enters the stable-definition
    hash and what is excluded, so exclusions are explicit and can never
    silently swallow a material job/prompt/provider/model/schedule change:
    only fields in ``_VOLATILE_FIELDS`` are excluded; every other field is
    either redacted (secret-bearing) or kept in full.
    """
    if key in _VOLATILE_FIELDS:
        return _FIELD_CLASS_RUNTIME_CLAIM
    if key in _REDACT_FIELDS:
        return _FIELD_CLASS_REDACTED
    return _FIELD_CLASS_STABLE


def _canonicalize_value(value: Any) -> Any:
    """Canonical representation of a non-redacted, non-volatile field value."""
    if isinstance(value, dict):
        return _json_dumps_canonical(value)
    if isinstance(value, list):
        return sorted(_json_dumps_canonical(item) for item in value)
    return value


def canonical_job_hash(job: Dict[str, Any]) -> str:
    """SHA-256 of a job's stable definition, redacting prompt/secret fields.

    The canonical view keeps identity, scheduling, scope and structural fields
    (so any definition drift is detected) while replacing ``prompt``, ``origin``,
    ``deliver`` and ``base_url`` with their SHA-256 digests and dropping
    volatile per-run counters and scheduler-injected runtime claim fields. No
    prompt/config/secret plaintext survives into the serialized string that is
    hashed.
    """
    if not isinstance(job, dict):
        return _sha256_text("null")

    canonical: Dict[str, Any] = {}
    for key in sorted(job.keys()):
        cls = classify_job_field(key)
        if cls == _FIELD_CLASS_RUNTIME_CLAIM:
            continue
        value = job.get(key)
        if cls == _FIELD_CLASS_REDACTED:
            canonical[f"{key}_sha256"] = _redacted_value_hash(value)
            continue
        canonical[key] = _canonicalize_value(value)
    return _sha256_text(_json_dumps_canonical(canonical))


def canonical_job_field_view(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Public-safe classified field map for one job.

    Every field is classified (``stable`` / ``redacted`` / ``runtime_claim``)
    and its redacted-or-canonical representation stored, so the receipt can
    reproduce the integrity hash and prove field-level deltas without ever
    persisting prompt/secret plaintext. ``None`` for a non-dict job.
    """
    if not isinstance(job, dict):
        return None

    view: Dict[str, Any] = {}
    for key in sorted(job.keys()):
        cls = classify_job_field(key)
        value = job.get(key)
        if cls == _FIELD_CLASS_RUNTIME_CLAIM:
            view[key] = {"class": cls}
        elif cls == _FIELD_CLASS_REDACTED:
            view[key] = {"class": cls, "sha256": _redacted_value_hash(value)}
        else:
            view[key] = {"class": cls, "value": _canonicalize_value(value)}
    return view


def diff_job_field_views(
    pre: Optional[Dict[str, Any]], post: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Classified field-level delta between pre and post canonical views.

    Proves every delta and classifies it: a changed ``runtime_claim`` field is
    expected per-run movement, while a changed ``stable`` or ``redacted`` field
    is material nonvolatile drift and is surfaced (fail-closed) rather than
    silently swallowed.
    """
    pre = pre if isinstance(pre, dict) else {}
    post = post if isinstance(post, dict) else {}

    changed: List[Dict[str, Any]] = []
    nonvolatile: List[str] = []
    for key in sorted(set(pre) | set(post)):
        p = pre.get(key)
        q = post.get(key)
        if p == q:
            continue
        p_cls = p.get("class") if isinstance(p, dict) else None
        q_cls = q.get("class") if isinstance(q, dict) else None
        cls = q_cls or p_cls
        changed.append({
            "field": key,
            "class": cls,
            "pre": "absent" if p is None else p,
            "post": "absent" if q is None else q,
        })
        if cls != _FIELD_CLASS_RUNTIME_CLAIM:
            nonvolatile.append(key)

    return {
        "schema_version": FIELD_DIFF_SCHEMA_VERSION,
        "changed_fields": changed,
        "nonvolatile_drift": nonvolatile,
        "drift_is_reconciled": not nonvolatile,
    }


def sibling_set_hash(siblings: List[Dict[str, Any]]) -> str:
    """SHA-256 over the sorted identity+hash of every sibling job.

    Bind each sibling by ``id`` and its own canonical hash so a sibling's
    definition drift is detected without exposing any sibling content.
    """
    entries = [
        {"id": str(job.get("id", "")), "sha256": canonical_job_hash(job)}
        for job in (siblings or [])
        if isinstance(job, dict)
    ]
    entries.sort(key=lambda e: e["id"])
    return _sha256_text(_json_dumps_canonical(entries))


def capture_integrity_hashes(
    job_id: Optional[str], selected_job: Optional[Dict[str, Any]],
    siblings: Optional[List[Dict[str, Any]]] = None,
    sibling_load_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the pre/post integrity hashes for one execution.

    ``selected_job_sha256`` / ``sibling_set_sha256`` are ``None`` (with a
    ``capture_error``) when the selected job could not be resolved — the
    fail-closed branch: an execution whose job identity is unknown must not
    claim integrity.

    ``siblings=None`` (or a non-empty ``sibling_load_error``) means the sibling
    store could not be *read* and must be recorded as unavailable, never as a
    proven empty sibling set: an unreadable store is indistinguishable from a
    real zero-sibling set, so treating it as empty would persist a success hash
    for input that was never actually verified.
    """
    errors: List[str] = []
    if not isinstance(selected_job, dict) or not selected_job.get("id"):
        errors.append("selected_job_unresolved")
    if selected_job is not None and selected_job.get("id") != job_id:
        errors.append("selected_job_id_mismatch")

    selected_hash = canonical_job_hash(selected_job) if isinstance(
        selected_job, dict
    ) else None
    selected_fields = canonical_job_field_view(selected_job) if isinstance(
        selected_job, dict
    ) else None

    if sibling_load_error is not None:
        sibling_hash = None
        errors.append("sibling_set_unavailable")
        errors.append(sibling_load_error)
    elif siblings is None:
        sibling_hash = None
        errors.append("sibling_set_unavailable")
    else:
        sibling_hash = sibling_set_hash(siblings)

    return {
        "schema_version": HASH_SCHEMA_VERSION,
        "job_id": job_id,
        "selected_job_sha256": selected_hash,
        "selected_job_fields": selected_fields,
        "sibling_set_sha256": sibling_hash,
        "capture_errors": errors,
    }


# ---------------------------------------------------------------------------
# cgroup v2 snapshot (pids / memory / events)
# ---------------------------------------------------------------------------


def own_cgroup_path() -> Optional[str]:
    """Return the cgroup v2 path for the calling process, or None."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^0::(.+)$", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _read_cgroup_text(name: str) -> Optional[str]:
    path = own_cgroup_path()
    if not path:
        return None
    try:
        return Path(f"/sys/fs/cgroup{path}/{name}").read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    try:
        return int(text.strip())
    except (ValueError, TypeError):
        return None


def _parse_key_int(text: Optional[str], key: str) -> Optional[int]:
    """Parse ``key N`` pairs from a cgroup ``*.events`` / counter file."""
    if not text:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            return _parse_int(parts[1])
    return None


def capture_cgroup_snapshot() -> Dict[str, Any]:
    """Read the live cgroup v2 pids/memory counters + events (best-effort)."""
    errors: List[str] = []
    if own_cgroup_path() is None:
        errors.append("cgroup_path_unavailable")

    def _read(name: str) -> Optional[str]:
        return _read_cgroup_text(name)

    pids_current = _parse_int(_read("pids.current"))
    pids_max = _parse_int(_read("pids.max"))
    pids_peak = _parse_int(_read("pids.peak"))
    pids_events_raw = _read("pids.events")
    pids_events_max = _parse_key_int(pids_events_raw, "max")

    memory_current = _parse_int(_read("memory.current"))
    memory_max = _parse_int(_read("memory.max"))
    memory_peak = _parse_int(_read("memory.peak"))
    memory_events_raw = _read("memory.events")
    memory_events = {
        "low": _parse_key_int(memory_events_raw, "low"),
        "high": _parse_key_int(memory_events_raw, "high"),
        "max": _parse_key_int(memory_events_raw, "max"),
        "oom": _parse_key_int(memory_events_raw, "oom"),
        "oom_kill": _parse_key_int(memory_events_raw, "oom_kill"),
        "oom_group_kill": _parse_key_int(memory_events_raw, "oom_group_kill"),
    }

    for name, value in (
        ("pids.current", pids_current), ("pids.max", pids_max),
        ("pids.peak", pids_peak), ("pids.events", pids_events_max),
        ("memory.current", memory_current), ("memory.max", memory_max),
        ("memory.peak", memory_peak),
    ):
        if value is None:
            errors.append(f"cgroup_{name.replace('.', '_')}_unavailable")

    # Every required member of the events files must be present. A truncated or
    # partially-readable events file (some counters parsed, some missing) must
    # not be mistaken for a complete snapshot — each missing required member is
    # explicit fail-closed evidence rather than a silent null.
    for key, value in memory_events.items():
        if value is None:
            errors.append(f"cgroup_memory_events_{key}_unavailable")

    return {
        "pids": {
            "current": pids_current,
            "max": pids_max,
            "peak": pids_peak,
            "events": {"max": pids_events_max},
        },
        "memory": {
            "current": memory_current,
            "max": memory_max,
            "peak": memory_peak,
            "events": memory_events,
        },
        "capture_errors": errors,
    }


# ---------------------------------------------------------------------------
# /proc process state (direct children, deleted-cwd, workdir residue)
# ---------------------------------------------------------------------------


def _list_proc_pids() -> List[int]:
    """List numeric /proc/<pid> directories (bounded by the live process table)."""
    pids: List[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if entry.is_dir() and entry.name.isdigit():
                pids.append(int(entry.name))
    except OSError:
        pass
    return pids


def _proc_ppid(pid: int) -> Optional[int]:
    """Read a process's parent PID from /proc/<pid>/stat (field 4)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The comm field (field 2) may contain spaces/parens; find the last ')'.
    after = stat.rfind(")")
    if after < 0:
        return None
    fields = stat[after + 2:].split()
    if len(fields) < 2:
        return None
    return _parse_int(fields[1])  # field 3 = state, field 4 = ppid


def _proc_cwd(pid: int) -> Optional[str]:
    """Read a process's current working directory via /proc/<pid>/cwd."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def count_direct_children(pid: int) -> int:
    """Count direct children (PPid == pid) of a process. Linux only."""
    count = 0
    for child in _list_proc_pids():
        if child == pid:
            continue
        if _proc_ppid(child) == pid:
            count += 1
    return count


def count_deleted_cwd_processes(exclude_pid: Optional[int] = None) -> int:
    """Count processes whose cwd was deleted out from under them."""
    count = 0
    for pid in _list_proc_pids():
        if pid == exclude_pid:
            continue
        cwd = _proc_cwd(pid)
        if cwd is not None and cwd.endswith(" (deleted)"):
            count += 1
    return count


def _is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def count_processes_under(root: Optional[str], exclude_pid: Optional[int] = None) -> int:
    """Count processes (other than ``exclude_pid``) whose cwd is under ``root``."""
    if not root:
        return 0
    count = 0
    for pid in _list_proc_pids():
        if pid == exclude_pid:
            continue
        cwd = _proc_cwd(pid)
        if cwd is not None and _is_under(cwd, root):
            count += 1
    return count


def capture_process_snapshot(pid: Optional[int]) -> Dict[str, Any]:
    """Direct children + deleted-cwd counts for the lease-owner process."""
    owner = pid if isinstance(pid, int) and pid > 0 else None
    errors: List[str] = []
    if owner is None:
        errors.append("lease_owner_pid_unavailable")

    direct_children = count_direct_children(owner) if owner is not None else None
    deleted_cwd = count_deleted_cwd_processes(exclude_pid=owner)

    if direct_children is None:
        errors.append("direct_children_unavailable")
    return {
        "direct_children": direct_children,
        "deleted_cwd_processes": deleted_cwd,
        "capture_errors": errors,
    }


def capture_residue(
    job: Optional[Dict[str, Any]], *, exclude_pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Capture job-workdir and task-workspace process residue (read-only)."""
    job_workdir = (job or {}).get("workdir") if isinstance(job, dict) else None
    if isinstance(job_workdir, str):
        job_workdir = job_workdir.strip() or None

    task_workspace = os.getenv("HERMES_KANBAN_WORKSPACE") or None

    job_residue = count_processes_under(job_workdir, exclude_pid=exclude_pid)
    task_residue = count_processes_under(task_workspace, exclude_pid=exclude_pid)

    return {
        "job_workdir": job_workdir,
        "job_workdir_residue": job_residue,
        "task_workspace": task_workspace,
        "task_workspace_residue": task_residue,
    }


def capture_resource_snapshot(
    job: Optional[Dict[str, Any]], *, pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Full pre-acquisition / post-release resource snapshot (fail-closed)."""
    cgroup = capture_cgroup_snapshot()
    process = capture_process_snapshot(pid)
    residue = capture_residue(job, exclude_pid=pid)

    errors: List[str] = []
    errors.extend(cgroup.get("capture_errors", []))
    errors.extend(process.get("capture_errors", []))
    # residue errors are structural (no probes to fail); nothing to merge.

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": _hermes_now().isoformat(),
        "lease_owner_pid": pid,
        "cgroup": {
            "pids": cgroup["pids"],
            "memory": cgroup["memory"],
        },
        "process": {
            "direct_children": process["direct_children"],
            "deleted_cwd_processes": process["deleted_cwd_processes"],
        },
        "residue": residue,
        "capture_errors": errors,
    }


# ---------------------------------------------------------------------------
# Gateway generation + lease identity
# ---------------------------------------------------------------------------


def resolve_gateway_generation() -> Optional[Dict[str, Any]]:
    """Resolve the gateway's generation fingerprint: {main_pid, starttime}.

    Reads ``{HERMES_HOME}/gateway.pid`` (a JSON record with ``pid`` and
    ``start_time``). Falls back to the calling process's own identity when the
    PID file is absent (cron runs inside the gateway process, so ``os.getpid``
    is the gateway main PID). Returns ``None`` when neither can be resolved.
    """
    from hermes_constants import get_hermes_home

    try:
        pid_path = get_hermes_home().resolve() / "gateway.pid"
        record = json.loads(pid_path.read_text(encoding="utf-8"))
        main_pid = int(record.get("pid"))
        starttime = record.get("start_time")
        if starttime is not None:
            starttime = int(starttime)
        if main_pid > 0 and starttime is not None:
            return {"main_pid": main_pid, "starttime": starttime}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    # Fallback: the ticker runs in the gateway process itself.
    try:
        from gateway.status import get_process_start_time
        main_pid = os.getpid()
        starttime = get_process_start_time(main_pid)
        if starttime is not None:
            return {"main_pid": main_pid, "starttime": starttime}
    except Exception:
        pass

    return None


def lease_owner_identity(process_id: str, pid: int, process_started_at: Optional[int]) -> Dict[str, Any]:
    """The immutable process/session identity that owns an execution's lease."""
    return {
        "process_id": process_id,
        "pid": pid,
        "process_started_at": process_started_at,
    }
