"""Immutable admission-to-release receipt for cron executions.

RED/GREEN coverage for the R22 repair: every natural execution must persist one
identity-bound receipt (admission + lease owner + release + immutable pre/post
resource snapshots + integrity hashes), readable idempotently without duplicate
records, and fail-closed when identity/snapshot data is missing or ambiguous.

The receipt-unit tests exercise :mod:`cron.receipt` directly (deterministic,
monkeypatched probes). The ledger tests exercise :mod:`cron.executions`
through the same create → run → finish → readback path the scheduler uses.
"""

from __future__ import annotations

import json
import sqlite3

import pytest


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def _job(**overrides) -> dict:
    base = {
        "id": "job-receipt",
        "name": "receipt job",
        "prompt": "do the thing",
        "skills": [],
        "schedule": {"kind": "interval", "minutes": 11520, "display": "every 11520m"},
        "enabled": True,
        "workdir": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# cron.receipt — deterministic unit tests
# ---------------------------------------------------------------------------


def test_canonical_job_hash_is_stable_and_detects_drift():
    from cron.receipt import canonical_job_hash

    job = _job()
    assert canonical_job_hash(job) == canonical_job_hash(job)
    assert len(canonical_job_hash(job)) == 64

    drifted = _job(prompt="a different instruction")
    assert canonical_job_hash(drifted) != canonical_job_hash(job)


def test_canonical_job_hash_redacts_prompt_and_secret_values():
    from cron.receipt import canonical_job_hash

    job = _job(prompt="TOP_SECRET_PROMPT", deliver="telegram:-100123", base_url="https://x:KEY@host")
    digest = canonical_job_hash(job)
    # The digest is a hex string; no plaintext prompt/secret can appear in it.
    assert "TOP_SECRET_PROMPT" not in digest
    assert "KEY" not in digest
    assert digest == canonical_job_hash(job)


def test_canonical_job_hash_ignores_volatile_runtime_fields():
    from cron.receipt import canonical_job_hash

    base = _job()
    fired = _job(
        next_run_at="2026-08-23T00:00:00+00:00",
        last_run_at="2026-08-15T00:00:00+00:00",
        last_status="ok",
        repeat={"times": None, "completed": 15},
        state="scheduled",
    )
    assert canonical_job_hash(base) == canonical_job_hash(fired)


def test_sibling_set_hash_detects_sibling_drift():
    from cron.receipt import sibling_set_hash

    sibling_a = _job(id="sib-a")
    sibling_b = _job(id="sib-b")
    assert sibling_set_hash([sibling_a, sibling_b]) == sibling_set_hash([sibling_b, sibling_a])
    drifted = _job(id="sib-b", prompt="changed sibling")
    assert sibling_set_hash([sibling_a, drifted]) != sibling_set_hash([sibling_a, sibling_b])


def test_capture_integrity_hashes_fails_closed_when_job_unresolved():
    from cron.receipt import capture_integrity_hashes

    result = capture_integrity_hashes("missing", None, [])
    assert result["selected_job_sha256"] is None
    assert "selected_job_unresolved" in result["capture_errors"]


def test_capture_integrity_hashes_flags_id_mismatch():
    from cron.receipt import capture_integrity_hashes

    result = capture_integrity_hashes("expected-id", _job(id="other-id"), [])
    assert "selected_job_id_mismatch" in result["capture_errors"]


def test_cgroup_snapshot_parses_counters(monkeypatch):
    from cron import receipt

    monkeypatch.setattr(receipt, "own_cgroup_path", lambda: "/test.slice")

    def fake_read(name):
        return {
            "pids.current": "13\n",
            "pids.max": "100\n",
            "pids.peak": "25\n",
            "pids.events": "max 0\n",
            "memory.current": "259280896\n",
            "memory.max": "1258291200\n",
            "memory.peak": "330194944\n",
            "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n",
        }.get(name)

    monkeypatch.setattr(receipt, "_read_cgroup_text", fake_read)

    snap = receipt.capture_cgroup_snapshot()
    assert snap["pids"]["current"] == 13
    assert snap["pids"]["max"] == 100
    assert snap["pids"]["peak"] == 25
    assert snap["pids"]["events"]["max"] == 0
    assert snap["memory"]["current"] == 259280896
    assert snap["memory"]["events"]["oom"] == 0
    assert snap["memory"]["events"]["oom_kill"] == 0
    assert snap["capture_errors"] == []


def test_cgroup_snapshot_fails_closed_when_path_unavailable(monkeypatch):
    from cron import receipt

    monkeypatch.setattr(receipt, "own_cgroup_path", lambda: None)
    snap = receipt.capture_cgroup_snapshot()
    assert "cgroup_path_unavailable" in snap["capture_errors"]
    assert snap["pids"]["current"] is None


def test_resource_snapshot_shape(monkeypatch):
    from cron import receipt

    monkeypatch.setattr(receipt, "capture_cgroup_snapshot", lambda: {
        "pids": {"current": 1, "max": 2, "peak": 3, "events": {"max": 0}},
        "memory": {"current": 1, "max": 2, "peak": 3, "events": {"oom": 0}},
        "capture_errors": [],
    })
    monkeypatch.setattr(receipt, "count_direct_children", lambda _pid: 7)
    monkeypatch.setattr(receipt, "count_deleted_cwd_processes", lambda **_kw: 0)
    monkeypatch.setattr(receipt, "count_processes_under", lambda *_a, **_kw: 0)

    snap = receipt.capture_resource_snapshot(_job(), pid=12345)
    assert snap["schema_version"] == "receipt-snapshot-v1"
    assert snap["lease_owner_pid"] == 12345
    assert snap["cgroup"]["pids"]["current"] == 1
    assert snap["process"]["direct_children"] == 7
    assert snap["process"]["deleted_cwd_processes"] == 0
    assert "job_workdir" in snap["residue"]
    assert snap["captured_at"]


# ---------------------------------------------------------------------------
# cron.executions — receipt ledger integration (RED → GREEN)
# ---------------------------------------------------------------------------


def test_create_execution_records_admission_and_lease_owner(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())

    admission = json.loads(record["admission"])
    assert admission["result"] == "admitted"
    assert admission["acquisition"] == "claimed"
    assert admission["admitted_at"]
    assert admission["lease_owner"]["process_id"] == record["process_id"]
    assert admission["lease_owner"]["pid"] == record["pid"]
    assert admission["lease_owner"]["process_started_at"] == record["process_started_at"]


def test_create_execution_records_pre_snapshot_and_hashes(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())

    pre_snapshot = json.loads(record["pre_snapshot"])
    assert pre_snapshot["schema_version"] == "receipt-snapshot-v1"
    assert set(pre_snapshot["cgroup"].keys()) == {"pids", "memory"}
    assert set(pre_snapshot["cgroup"]["pids"].keys()) == {"current", "max", "peak", "events"}
    assert "direct_children" in pre_snapshot["process"]
    assert "deleted_cwd_processes" in pre_snapshot["process"]
    assert "job_workdir" in pre_snapshot["residue"]
    assert pre_snapshot["captured_at"]

    pre_hashes = json.loads(record["pre_hashes"])
    assert pre_hashes["schema_version"] == "receipt-hash-v1"
    assert pre_hashes["job_id"] == "job-receipt"
    assert pre_hashes["selected_job_sha256"]
    assert pre_hashes["sibling_set_sha256"]
    assert pre_hashes["capture_errors"] == []


def test_create_execution_records_gateway_generation(monkeypatch, tmp_path):
    import cron.executions as executions_mod

    monkeypatch.setattr(
        executions_mod, "resolve_gateway_generation",
        lambda: {"main_pid": 1917036, "starttime": 213871581},
    )
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())
    assert json.loads(record["gateway_generation"]) == {"main_pid": 1917036, "starttime": 213871581}


def test_finish_execution_records_release_and_post_receipt(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())
    executions.mark_execution_running(record["id"])

    completed = executions.finish_execution(record["id"], success=True, job=_job())

    release = json.loads(completed["release"])
    assert release["result"] == "completed"
    assert release["released_at"]
    assert release["lease_owner"]["process_id"] == record["process_id"]
    assert release["lease_owner"]["pid"] == record["pid"]
    assert release["process_closure"]["closed"] is True

    post_snapshot = json.loads(completed["post_snapshot"])
    assert post_snapshot["schema_version"] == "receipt-snapshot-v1"
    assert post_snapshot["captured_at"]

    post_hashes = json.loads(completed["post_hashes"])
    assert post_hashes["selected_job_sha256"]
    assert post_hashes["capture_errors"] == []


def test_failure_path_persists_fail_closed_receipt(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())
    failed = executions.finish_execution(record["id"], success=False, error="boom", job=_job())

    release = json.loads(failed["release"])
    assert release["result"] == "failed"
    assert release["released_at"]
    assert failed["error"] == "boom"
    # A failure still persists the post-release snapshot + hashes.
    assert json.loads(failed["post_snapshot"])["captured_at"]
    assert json.loads(failed["post_hashes"])["selected_job_sha256"]


def test_receipt_binds_identity_and_ordered_timestamps(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())
    executions.mark_execution_running(record["id"])
    completed = executions.finish_execution(record["id"], success=True, job=_job())

    assert completed["job_id"] == "job-receipt"
    assert completed["id"] == record["id"]
    assert completed["source"] == "builtin"

    admission = json.loads(completed["admission"])
    release = json.loads(completed["release"])
    pre = json.loads(completed["pre_snapshot"])
    post = json.loads(completed["post_snapshot"])

    # ordered: pre-admission -> admitted -> released -> post-release
    assert admission["admitted_at"] <= release["released_at"]
    assert pre["captured_at"] <= post["captured_at"]


def test_one_logical_receipt_per_execution_and_idempotent_readback(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True, job=_job())

    # Terminal receipt is immutable: a second finish is a no-op.
    assert executions.finish_execution(record["id"], success=False, error="late") is None

    records = executions.list_executions(job_id="job-receipt")
    assert len(records) == 1
    assert records[0]["id"] == record["id"]
    assert records[0]["status"] == "completed"
    assert json.loads(records[0]["admission"])["result"] == "admitted"


def test_receipt_fails_closed_when_job_unresolved(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    # No job passed, and the store has no matching job: integrity must fail closed.
    record = executions.create_execution("no-such-job", source="builtin")
    pre_hashes = json.loads(record["pre_hashes"])
    assert pre_hashes["selected_job_sha256"] is None
    assert "selected_job_unresolved" in pre_hashes["capture_errors"]


def test_receipt_never_persists_prompt_or_secret_plaintext(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    job = _job(prompt="TOP_SECRET_INSTRUCTION", deliver="telegram:-100123")
    record = executions.create_execution("job-receipt", source="builtin", job=job)
    executions.finish_execution(record["id"], success=True, job=job)

    blob = json.dumps({
        "pre_hashes": record["pre_hashes"],
        "post_hashes": executions.latest_execution("job-receipt")["post_hashes"],
    })
    assert "TOP_SECRET_INSTRUCTION" not in blob
    assert "telegram:-100123" not in blob


def test_migration_adds_receipt_columns_to_existing_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    db = tmp_path / "cron" / "executions.db"
    db.parent.mkdir(parents=True)
    # Simulate a pre-R22 ledger: the base schema without any receipt columns.
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE executions (
             id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
             process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
             status TEXT NOT NULL, claimed_at TEXT NOT NULL,
             started_at TEXT, finished_at TEXT, error TEXT
           )"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", db)
    record = executions.create_execution("job-receipt", source="builtin", job=_job())
    assert "admission" in record
    assert "release" in record
    assert "pre_snapshot" in record
    assert "post_snapshot" in record
    assert "pre_hashes" in record
    assert "post_hashes" in record
    assert "gateway_generation" in record
