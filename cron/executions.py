"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue.
Interrupted attempts become ``unknown`` only after their exact owner process is
proved gone. Terminal states are immutable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from cron.receipt import (
    capture_integrity_hashes,
    capture_resource_snapshot,
    diff_job_field_views,
    lease_owner_identity,
    resolve_gateway_generation,
)

EXECUTIONS_FILE = get_hermes_home().resolve() / "cron" / "executions.db"
MAX_TERMINAL_EXECUTIONS = 1000
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex

# Receipt columns added for immutable admission-to-release evidence. Each holds
# a JSON string (or NULL). They are appended to an existing ``executions`` DB
# via a guarded ALTER so older ledgers migrate in place without a rebuild.
_RECEIPT_COLUMNS = (
    "gateway_generation",
    "admission",
    "pre_snapshot",
    "pre_hashes",
    "release",
    "post_snapshot",
    "post_hashes",
    "session_id",
)


def _connect() -> sqlite3.Connection:
    EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(EXECUTIONS_FILE, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/executions.db")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    _migrate_receipt_columns(conn)


def _migrate_receipt_columns(conn: sqlite3.Connection) -> None:
    """Add any missing receipt columns to an existing executions table.

    ``CREATE TABLE IF NOT EXISTS`` never extends a pre-existing table, so a
    ledger written by an older Hermes release must be migrated in place. Each
    column is appended only when absent; the migration is idempotent and never
    rewrites existing rows.
    """
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(executions)").fetchall()
    }
    for column in _RECEIPT_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE executions ADD COLUMN {column} TEXT")


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back
    the transaction; it does not close the connection. Relying on that alone
    leaks a connection (and its WAL/SHM file descriptors) on every call,
    since closing then depends on the garbage collector. Schema init runs
    inside the ``try`` too, so a PRAGMA/DDL failure after a successful
    ``connect()`` still closes the connection instead of leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )


def _resolve_job_and_siblings(
    job_id: Optional[str], job: Optional[Dict[str, Any]]
) -> "tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]":
    """Resolve the selected job and its siblings for integrity hashing.

    Prefers the caller-supplied ``job`` (the scheduler already has it in hand)
    and only falls back to a fresh store load when it is absent or mismatched.
    Returns ``(selected_job, siblings, sibling_load_error)``. When the job
    store cannot be read, ``siblings`` is empty but ``sibling_load_error`` is a
    non-empty marker so the hash layer records the sibling set as *unavailable*
    (fail-closed) rather than as a proven empty sibling set.
    """
    sibling_load_error: Optional[str] = None
    try:
        from cron.jobs import load_jobs

        jobs = load_jobs()
    except Exception as exc:
        jobs = []
        sibling_load_error = f"sibling_store_unavailable:{type(exc).__name__}"

    selected = job
    if not isinstance(selected, dict) or selected.get("id") != job_id:
        selected = next((j for j in jobs if j.get("id") == job_id), None)
    siblings = [j for j in jobs if j.get("id") != job_id]
    return selected, siblings, sibling_load_error


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def create_execution(
    job_id: str, *, source: str, job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch.

    In addition to the base identity, this records the pre-acquisition receipt:
    gateway generation, the admission/lease record, an immutable resource
    snapshot taken *before* the run, and integrity hashes of the selected job
    and its siblings. Captures happen outside the ledger lock so a slow
    ``/proc`` walk never stalls the write.
    """
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    process_started_at = _process_start_time(pid)

    lease_owner = lease_owner_identity(_PROCESS_ID, pid, process_started_at)
    gateway_generation = resolve_gateway_generation()
    selected_job, siblings, sibling_load_error = _resolve_job_and_siblings(job_id, job)

    # The pre-acquisition snapshot is captured *before* admission so its
    # ``captured_at`` is deterministically <= ``admitted_at``. The ordering is
    # then machine-validated below and any inversion (e.g. a clock adjustment)
    # is recorded as explicit fail-closed evidence instead of being left
    # ambiguous.
    pre_snapshot_obj = capture_resource_snapshot(selected_job, pid=pid)
    now = _hermes_now().isoformat()

    captured_at = pre_snapshot_obj.get("captured_at")
    if captured_at is not None and str(captured_at) > now:
        pre_snapshot_obj["capture_errors"] = list(
            pre_snapshot_obj.get("capture_errors", [])
        ) + ["pre_admission_ordering_violation"]

    admission = _json_dump({
        "result": "admitted",
        "admitted_at": now,
        "acquisition": "claimed",
        "lease_owner": lease_owner,
    })
    pre_snapshot = _json_dump(pre_snapshot_obj)
    pre_hashes = _json_dump(capture_integrity_hashes(
        job_id, selected_job, siblings, sibling_load_error=sibling_load_error,
    ))
    gateway_generation_json = _json_dump(gateway_generation) if gateway_generation else None

    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, gateway_generation, admission,
                pre_snapshot, pre_hashes)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             process_started_at, now, gateway_generation_json,
             admission, pre_snapshot, pre_hashes),
        )
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    return _record(row)  # type: ignore[return-value]


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?
               WHERE id=? AND status='claimed'""",
            (now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def _append_pre_capture_error(
    conn: sqlite3.Connection, execution_id: str,
    raw_pre_hashes: Optional[str], error: str,
) -> None:
    """Append a capture error to the stored pre-hash receipt in place.

    Used to persist fail-closed evidence (e.g. a session-id substitution) at
    bind time, before a post-receipt exists. Operates on the caller's already
    open connection inside the current transaction; never raises outward.
    """
    try:
        obj = json.loads(raw_pre_hashes) if raw_pre_hashes else {}
    except (ValueError, TypeError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    errors = list(obj.get("capture_errors", []))
    if error not in errors:
        errors.append(error)
    obj["capture_errors"] = errors
    conn.execute(
        "UPDATE executions SET pre_hashes=? WHERE id=?",
        (_json_dump(obj), execution_id),
    )


def bind_execution_session(
    execution_id: str, session_id: str,
) -> Optional[Dict[str, Any]]:
    """Durably bind the exact natural cron session identity to an execution.

    Called as soon as the natural session id is created and BEFORE provider /
    model execution, so a hard gateway/process interruption between session
    creation and finish can never lose the binding: the id is already on the
    immutable row when recovery later terminalizes the abandoned attempt.

    Idempotent and substitution-safe: ``COALESCE(session_id, ?)`` keeps the
    first bound id, so a later conflicting id never overwrites it. A
    conflicting second bind is still recorded as explicit
    ``session_id_substitution`` fail-closed evidence on the stored pre-receipt
    (there is no post-receipt yet at bind time) so a substitution is never
    silent.
    """
    incoming = str(session_id).strip()
    if not incoming:
        return None
    with _transaction() as conn:
        stored = conn.execute(
            "SELECT session_id, pre_hashes FROM executions WHERE id=?",
            (execution_id,),
        ).fetchone()
        if stored is None:
            return None
        existing = stored["session_id"]
        if existing and existing != incoming:
            # Conflicting id — never overwrite; record fail-closed evidence.
            _append_pre_capture_error(
                conn, execution_id, stored["pre_hashes"], "session_id_substitution",
            )
        else:
            conn.execute(
                "UPDATE executions SET session_id=COALESCE(session_id, ?) "
                "WHERE id=? AND status IN ('claimed','running')",
                (incoming, execution_id),
            )
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    job: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    session_required: bool = False,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten.

    On the write path this also records the post-release receipt: the release
    record (lease owner + process closure) and an immediate post-release
    resource snapshot + integrity hashes, so a timeout/failure/retry that
    reaches this function still persists a fail-closed receipt.

    The post-receipt is bound to the execution's *stored* job identity, not the
    caller-supplied job: a caller finishing one execution with a different job
    cannot substitute a foreign identity into the immutable receipt.

    ``session_id`` (when provided) is the exact natural cron session identity.
    It is bound into the immutable row exactly once; a later conflicting id is
    rejected (never overwrites the already-bound value) and recorded as an
    explicit ``session_id_substitution`` fail-closed error.

    ``session_required`` declares that this execution is an agent-backed run
    that must carry a natural session identity. When True and the row
    terminalizes with no session id (neither stored nor incoming), the omission
    is recorded as an explicit ``session_id_missing`` fail-closed error instead
    of a silently NULL session.
    """
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")

    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    lease_owner = lease_owner_identity(_PROCESS_ID, pid, process_started_at)
    release = _json_dump({
        "result": status,
        "released_at": now,
        "lease_owner": lease_owner,
        "process_closure": {
            "closed": True,
            "closed_at": now,
            "pid_alive": _owner_is_live(pid, process_started_at),
        },
    })

    # Bind the post-receipt to the execution's *stored* job identity, not the
    # caller-supplied job. The read and the terminal write share one connection
    # (and one lock hold) so a single finish still opens exactly one connection.
    with _transaction() as conn:
        stored = conn.execute(
            "SELECT job_id, pre_hashes, session_id FROM executions WHERE id=?",
            (execution_id,),
        ).fetchone()
        if stored is None:
            return None
        stored_job_id = stored["job_id"]

        caller_job_id = job.get("id") if isinstance(job, dict) else None
        identity_mismatch = caller_job_id is not None and str(caller_job_id) != stored_job_id
        # On a caller identity mismatch, ignore the foreign job and resolve the
        # stored identity from the store; the mismatch itself is recorded below
        # as fail-closed evidence so a substitution can never silently succeed.
        resolved_job = None if identity_mismatch else job
        selected_job, siblings, sibling_load_error = _resolve_job_and_siblings(
            stored_job_id, resolved_job
        )
        post_snapshot = _json_dump(capture_resource_snapshot(selected_job, pid=pid))
        post_hashes_obj = capture_integrity_hashes(
            stored_job_id, selected_job, siblings, sibling_load_error=sibling_load_error,
        )
        if identity_mismatch:
            post_hashes_obj["capture_errors"] = list(
                post_hashes_obj.get("capture_errors", [])
            ) + ["caller_job_id_mismatch"]

        # Classified field-level delta between the admission (pre) and release
        # (post) canonical views. This proves every per-run delta and, when any
        # nonvolatile (stable/redacted) field moved, surfaces it as explicit
        # fail-closed evidence instead of leaving the pre/post hash drift opaque.
        pre_fields = None
        try:
            stored_pre_hashes = json.loads(stored["pre_hashes"]) if stored["pre_hashes"] else None
            if isinstance(stored_pre_hashes, dict):
                pre_fields = stored_pre_hashes.get("selected_job_fields")
        except (ValueError, TypeError):
            pre_fields = None
        field_diff = diff_job_field_views(
            pre_fields, post_hashes_obj.get("selected_job_fields")
        )
        post_hashes_obj["selected_job_field_diff"] = field_diff
        if field_diff.get("nonvolatile_drift"):
            post_hashes_obj["capture_errors"] = list(
                post_hashes_obj.get("capture_errors", [])
            ) + ["nonvolatile_job_drift"]

        # Bind the exact natural cron session identity exactly once. A second
        # write with a different id can never overwrite the bound value
        # (COALESCE below keeps the first); the mismatch is still recorded as
        # explicit fail-closed evidence so a substitution is never silent.
        incoming_session_id = str(session_id).strip() if session_id else None
        stored_session_id = stored["session_id"]
        if incoming_session_id and stored_session_id and stored_session_id != incoming_session_id:
            post_hashes_obj["capture_errors"] = list(
                post_hashes_obj.get("capture_errors", [])
            ) + ["session_id_substitution"]
        # Fail-closed omission: a required (agent-backed) execution must never
        # terminalize with session_id still NULL and no evidence. When the
        # caller declares the session required and neither the stored row nor
        # the incoming caller supplied an identity, record it explicitly rather
        # than silently completing without one (R25 omission fail-open).
        bound_session_id = stored_session_id or incoming_session_id
        if session_required and not bound_session_id:
            post_hashes_obj["capture_errors"] = list(
                post_hashes_obj.get("capture_errors", [])
            ) + ["session_id_missing"]
        post_hashes = _json_dump(post_hashes_obj)

        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, release=?, post_snapshot=?, post_hashes=?,
                   session_id=COALESCE(session_id, ?)
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail, release, post_snapshot, post_hashes,
             incoming_session_id, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries.

    The recovered execution still receives a complete terminal receipt: an
    identity-bound release record (the abandoned owner's original identity) and
    an immediate post-release snapshot + integrity hashes. Exact evidence that
    is unavailable after the owner exited is recorded as explicit capture
    errors rather than a silently null terminal disposition.
    """
    now = _hermes_now().isoformat()
    error_msg = (
        "Scheduler restarted after this execution's owner exited before a durable "
        "terminal state; whether side effects ran is unknown."
    )
    # Phase 1: identify abandoned rows under a short lock (owner-liveness checks
    # and /proc walks are kept out of the write lock below).
    abandoned: List[Dict[str, Any]] = []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, job_id, process_id, pid, process_started_at FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            abandoned.append({
                "id": row["id"],
                "job_id": row["job_id"],
                "process_id": row["process_id"],
                "pid": int(row["pid"]),
                "process_started_at": row["process_started_at"],
            })

    changed = 0
    for row in abandoned:
        # Bind the release to the abandoned owner's *original* identity, not the
        # recovering process's.
        lease_owner = lease_owner_identity(
            row["process_id"], row["pid"], row["process_started_at"]
        )
        release = _json_dump({
            "result": "unknown",
            "released_at": now,
            "lease_owner": lease_owner,
            "process_closure": {
                "closed": True,
                "closed_at": now,
                "pid_alive": False,
                "reason": "owner_exited_before_terminal_state",
            },
        })
        selected_job, siblings, sibling_load_error = _resolve_job_and_siblings(
            row["job_id"], None
        )
        post_snapshot = _json_dump(capture_resource_snapshot(selected_job, pid=row["pid"]))
        post_hashes = _json_dump(capture_integrity_hashes(
            row["job_id"], selected_job, siblings, sibling_load_error=sibling_load_error,
        ))

        with _transaction() as conn:
            cur = conn.execute(
                """UPDATE executions
                   SET status='unknown', finished_at=?, error=?, release=?, post_snapshot=?, post_hashes=?
                   WHERE id=? AND status IN ('claimed','running')""",
                (now, error_msg, release, post_snapshot, post_hashes, row["id"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                _prune_unlocked(conn)
    return changed


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
