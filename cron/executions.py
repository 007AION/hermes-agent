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
) -> "tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]":
    """Resolve the selected job and its siblings for integrity hashing.

    Prefers the caller-supplied ``job`` (the scheduler already has it in hand)
    and only falls back to a fresh store load when it is absent or mismatched.
    Never raises: a store that cannot be read simply yields no siblings, which
    the hash layer records as a fail-closed capture error.
    """
    try:
        from cron.jobs import load_jobs

        jobs = load_jobs()
    except Exception:
        jobs = []

    selected = job
    if not isinstance(selected, dict) or selected.get("id") != job_id:
        selected = next((j for j in jobs if j.get("id") == job_id), None)
    siblings = [j for j in jobs if j.get("id") != job_id]
    return selected, siblings


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
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    process_started_at = _process_start_time(pid)

    lease_owner = lease_owner_identity(_PROCESS_ID, pid, process_started_at)
    gateway_generation = resolve_gateway_generation()
    admission = _json_dump({
        "result": "admitted",
        "admitted_at": now,
        "acquisition": "claimed",
        "lease_owner": lease_owner,
    })
    selected_job, siblings = _resolve_job_and_siblings(job_id, job)
    pre_snapshot = _json_dump(capture_resource_snapshot(selected_job, pid=pid))
    pre_hashes = _json_dump(capture_integrity_hashes(job_id, selected_job, siblings))
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


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    job: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten.

    On the write path this also records the post-release receipt: the release
    record (lease owner + process closure) and an immediate post-release
    resource snapshot + integrity hashes, so a timeout/failure/retry that
    reaches this function still persists a fail-closed receipt.
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

    job_id = job.get("id") if isinstance(job, dict) else None
    selected_job, siblings = _resolve_job_and_siblings(job_id, job)
    post_snapshot = _json_dump(capture_resource_snapshot(selected_job, pid=pid))
    post_hashes = _json_dump(capture_integrity_hashes(job_id, selected_job, siblings))

    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, release=?, post_snapshot=?, post_hashes=?
               WHERE id=? AND status IN ('claimed','running')""",
            (status, now, detail, release, post_snapshot, post_hashes, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        return _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, process_id, pid, process_started_at FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            cur = conn.execute(
                """UPDATE executions SET status='unknown', finished_at=?, error=?
                   WHERE id=? AND status IN ('claimed','running')""",
                (now,
                 "Scheduler restarted after this execution's owner exited before a durable "
                 "terminal state; whether side effects ran is unknown.",
                 row["id"]),
            )
            changed += cur.rowcount
        if changed:
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
