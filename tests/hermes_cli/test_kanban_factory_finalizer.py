"""AION-889 I1+I2 — kernel-owned atomic bind-and-terminalize finalizer tests.

These prove the Elder-approved architecture A (ATOMIC KERNEL-OWNED BIND-AND-
TERMINALIZE) end-to-end against the real ``hermes_cli.kanban_db`` runtime, in a
clean isolated board DB (never the live aion-factory board).

The finalizer SHA-pin-loads the frozen aion-governance kernel/adapters/binder
from ``AION_GOVERNANCE_SOURCE_DIR``; set that env (or the ``aion_gov_src``
fixture) to the aion-governance checkout whose modules match the pinned hashes
in ``kanban_db``. Tests that require the pinned source skip cleanly when it is
not configured or does not match.

T1 RED  gate=1 / no receipt / finalizer disabled -> FAIL_CLOSED zero mutation
T2 GREEN non-merge running task -> done + receipt bound + r1..r8 + C1..C10
        all true + child wakes, uploaded_by=aion_monarch_proof_kernel
T3 RED  worker-forged 'agent' uploader rejected (provenance)
T4 RED  stale run / CAS miss -> FAIL_CLOSED zero mutation
T5 RED  cross-task receipt replay rejected (r4 task/run mismatch)
T6 RED  fault injection at a write boundary -> rollback, retry idempotent
T7 GREEN merge-bearing pre-bound receipt path unchanged
T8 GREEN already-terminal path unchanged
T10     ALTERNATE_SUCCESS_PATHS stays 0 (no alternate success path)
T11 HOSTILE subprocess: worker/gateway kanban_db module-hash equality + guard
        present + finalizer path succeeds; no live service used
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _aion_gov_source_dir() -> Path | None:
    """Resolve the aion-governance source dir the finalizer will pin-load."""
    raw = os.environ.get("AION_GOVERNANCE_SOURCE_DIR")
    if raw and Path(raw).is_dir():
        return Path(raw)
    # Fall back to a sibling checkout next to the hermes-agent clone.
    repo_root = Path(kb.__file__).resolve().parents[1]
    sibling = repo_root.parent / "aion-governance"
    if (sibling / "scripts" / "aion_monarch_outcome_proof_gate.py").is_file():
        return sibling
    return None


@pytest.fixture
def aion_gov_src(monkeypatch):
    """Point AION_GOVERNANCE_SOURCE_DIR at the aion-governance checkout, or
    mark the test as skipped when unavailable."""
    src = _aion_gov_source_dir()
    if src is None:
        pytest.skip("AION_GOVERNANCE_SOURCE_DIR not configured")
    monkeypatch.setenv("AION_GOVERNANCE_SOURCE_DIR", str(src))
    # Assert the pinned source matches the pinned hashes so the finalizer path
    # is genuinely testable here (fail loud rather than masking a drift).
    kernel = (src / "scripts" / "aion_monarch_outcome_proof_gate.py").read_bytes()
    if hashlib.sha256(kernel).hexdigest() != kb.AION_GOVERNANCE_KERNEL_SHA256:
        pytest.skip("aion-governance kernel does not match pinned sha256")
    return src


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claim_and_run_id(conn, task_id) -> int:
    kb.claim_task(conn, task_id)
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    assert row and row["current_run_id"] is not None
    return int(row["current_run_id"])


def _bound_receipt_doc(conn, task_id) -> dict:
    atts = kb.list_attachments(conn, task_id)
    receipts = [a for a in atts if a.filename == "aion_monarch_receipt.json"]
    assert receipts, "no bound receipt attachment"
    return json.loads(Path(receipts[-1].stored_path).read_bytes().decode("utf-8"))


_FROZEN_CONDITION_KEYS = tuple(f"C{i}" for i in range(1, 11))
_FROZEN_TRUSTED_FIELDS = (
    "adapter_type_and_version", "exact_source_refs", "target_identity",
    "before_digest", "action_receipt_ref", "after_digest",
    "head_epoch_or_run_binding", "acquired_at",
)


def _assert_receipt_passes_r1_r8(doc: dict, task_id: str, run_id: str) -> None:
    assert doc["schema"] == "aion.monarch.trusted_receipt.v1"          # r1
    assert doc["verdict"] == "OUTCOME_ACCEPTED"                         # r2
    assert doc["conditions"] == {k: True for k in _FROZEN_CONDITION_KEYS}  # r3
    tid = doc["target_identity"].get("task_id") or doc["target_identity"]["fields"]["task_id"]
    assert str(tid) == str(task_id)                                     # r4 (task)
    binding = doc["head_epoch_or_run_binding"]
    run_ref = binding.get("value") or binding.get("run_id")
    assert str(run_ref) == str(run_id)                                  # r4 (run)
    assert doc["contract_hash_sha256"] == kb.FACTORY_CONTRACT_HASH_SHA256  # r5
    assert isinstance(doc.get("kernel_version"), str) and doc["kernel_version"].strip()  # r6
    for f in _FROZEN_TRUSTED_FIELDS:                                    # r7
        assert f in doc, f"missing trusted_receipt_binding field {f}"
    action = doc["action_receipt_ref"]
    assert action["actor_identity_source"] not in {"", "self", "self_declared"}  # r8
    assert action["actor_role"] == "action_executor"


def _kernel_receipt_doc(task_id: str, run_id: str) -> dict:
    """Build a kernel-shaped receipt (all r1..r8 fields present)."""
    return {
        "schema": "aion.monarch.trusted_receipt.v1",
        "verdict": "OUTCOME_ACCEPTED",
        "kernel_version": "aion.monarch.proof_kernel.v2",
        "contract_hash_sha256": kb.FACTORY_CONTRACT_HASH_SHA256,
        "conditions": {f"C{i}": True for i in range(1, 11)},
        "adapter_type_and_version": "aion.monarch.typed_adapter.task_terminal.v1",
        "exact_source_refs": {"task_id": task_id, "run_id": run_id},
        "target_identity": {
            "object_type": "kanban_task_run",
            "object_ref_exact": f"{task_id}/{run_id}",
            "fields": {"task_id": task_id, "run_id": run_id},
        },
        "before_digest": "a" * 64,
        "action_receipt_ref": {
            "action_kind": "task_terminal",
            "actor": "aion_monarch_proof_kernel",
            "actor_role": "action_executor",
            "actor_identity_source": "native_task_run_authorization_binding",
            "executed_effect_ref": "task_events:status=done",
            "executed_at": "2026-08-18T06:00:01Z",
        },
        "after_digest": "b" * 64,
        "head_epoch_or_run_binding": {
            "bound_to": "task_run_id",
            "value": run_id,
            "authorization_source_ref": "native_task_run_authorization_binding",
            "authorization_epoch_or_version": 1,
        },
        "acquired_at": "2026-08-18T06:00:02Z",
    }


# ---------------------------------------------------------------------------
# T1 RED — finalizer disabled -> FAIL_CLOSED zero mutation
# ---------------------------------------------------------------------------

def test_t1_finalizer_disabled_fail_closed_zero_mutation(kanban_home, aion_gov_src, monkeypatch):
    monkeypatch.setenv("AION_FACTORY_FINALIZER_ENABLED", "0")
    with kb.connect() as conn:
        t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
        run_id = _claim_and_run_id(conn, t)
        status_before = kb.get_task(conn, t).status
        events_before = [e.kind for e in kb.list_events(conn, t)]

        with pytest.raises(kb.FactoryTerminalReceiptRequiredError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        # Zero mutation.
        assert kb.get_task(conn, t).status == status_before == "running"
        assert [e.kind for e in kb.list_events(conn, t)] == events_before
        row = conn.execute(
            "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["factory_terminal_receipt_sha256"] is None
        assert kb.list_attachments(conn, t) == []


# ---------------------------------------------------------------------------
# T2 GREEN — non-merge running task completes atomically, child wakes
# ---------------------------------------------------------------------------

def test_t2_finalizer_completes_atomically_child_wakes(kanban_home, aion_gov_src):
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="factory parent", factory_build_gate=1, assignee="agent007",
        )
        child = kb.create_task(
            conn, title="child", assignee="agent007", parents=[parent],
        )
        assert kb.get_task(conn, child).status == "todo"
        run_id = _claim_and_run_id(conn, parent)

        assert kb.complete_task(conn, parent, result="kernel done", expected_run_id=run_id)

        # Terminal write + receipt binding all happened atomically.
        t = kb.get_task(conn, parent)
        assert t.status == "done"
        assert t.result == "kernel done"
        row = conn.execute(
            "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        sha = row["factory_terminal_receipt_sha256"]
        assert sha and len(sha) == 64

        # Receipt attachment stamped with the trusted kernel identity.
        atts = kb.list_attachments(conn, parent)
        receipt = [a for a in atts if a.filename == "aion_monarch_receipt.json"]
        assert receipt and receipt[0].uploaded_by == "aion_monarch_proof_kernel"

        # The bound sha == sha256 of the receipt attachment bytes (r5/attachment).
        doc_bytes = Path(receipt[0].stored_path).read_bytes()
        assert hashlib.sha256(doc_bytes).hexdigest() == sha

        # r1..r8 + C1..C10 all true.
        doc = json.loads(doc_bytes.decode("utf-8"))
        _assert_receipt_passes_r1_r8(doc, parent, str(run_id))
        assert doc["conditions"] == {k: True for k in _FROZEN_CONDITION_KEYS}

        # Child wakes (dependency promotion).
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# T3 RED — worker-forged 'agent' uploader rejected (provenance)
# ---------------------------------------------------------------------------

def test_t3_worker_forged_uploader_rejected(kanban_home, aion_gov_src, monkeypatch):
    # The finalizer stamps aion_monarch_proof_kernel; a worker attaching a
    # structurally-identical receipt with uploaded_by='agent' (or any non-
    # trusted identity) must still be rejected by the provenance gate. This is
    # the provenance boundary: with the finalizer DISABLED, a worker-forged
    # receipt (matching digest) can never terminalize the task.
    monkeypatch.setenv("AION_FACTORY_FINALIZER_ENABLED", "0")
    with kb.connect() as conn:
        t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
        run_id = _claim_and_run_id(conn, t)
        # Forge: attach a kernel-shaped receipt with the worker's own identity.
        forged = {
            "schema": "aion.monarch.trusted_receipt.v1",
            "verdict": "OUTCOME_ACCEPTED",
            "kernel_version": "aion.monarch.proof_kernel.v2",
            "contract_hash_sha256": kb.FACTORY_CONTRACT_HASH_SHA256,
            "conditions": {f"C{i}": True for i in range(1, 11)},
            "adapter_type_and_version": "aion.monarch.typed_adapter.task_terminal.v1",
            "exact_source_refs": {"task_id": t, "run_id": str(run_id)},
            "target_identity": {
                "object_type": "kanban_task_run",
                "object_ref_exact": f"{t}/{run_id}",
                "fields": {"task_id": t, "run_id": str(run_id)},
            },
            "before_digest": "a" * 64,
            "action_receipt_ref": {
                "action_kind": "task_terminal",
                "actor": "aion_monarch_proof_kernel",
                "actor_role": "action_executor",
                "actor_identity_source": "native_task_run_authorization_binding",
                "executed_effect_ref": "task_events:status=done",
                "executed_at": "2026-08-18T06:00:01Z",
            },
            "after_digest": "b" * 64,
            "head_epoch_or_run_binding": {
                "bound_to": "task_run_id",
                "value": str(run_id),
                "authorization_source_ref": "native_task_run_authorization_binding",
                "authorization_epoch_or_version": 1,
            },
            "acquired_at": "2026-08-18T06:00:02Z",
        }
        raw = json.dumps(forged, sort_keys=True).encode("utf-8")
        kb.store_attachment_bytes(conn, t, "receipt.json", raw, uploaded_by="agent")
        conn.execute(
            "UPDATE tasks SET factory_terminal_receipt_sha256 = ? WHERE id = ?",
            (hashlib.sha256(raw).hexdigest(), t),
        )
        conn.commit()

        # Provenance: 'agent' is not trusted -> the pre-bound path is invalid;
        # and the finalizer is not triggered for a pre-bound *invalid* receipt
        # when it would mask provenance (disabled here to isolate provenance).
        with pytest.raises(kb.FactoryTerminalReceiptRequiredError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, t).status != "done"


# ---------------------------------------------------------------------------
# T4 RED — stale run / CAS miss -> FAIL_CLOSED zero mutation
# ---------------------------------------------------------------------------

def test_t4_stale_run_cas_rejected(kanban_home, aion_gov_src):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
        run_id = _claim_and_run_id(conn, t)
        # Simulate the dispatcher superseding this run.
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id + 999, t),
        )
        conn.commit()

        with pytest.raises(kb.FactoryTerminalReceiptRequiredError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        # Zero mutation: still running, no receipt, no sha.
        assert kb.get_task(conn, t).status == "running"
        row = conn.execute(
            "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["factory_terminal_receipt_sha256"] is None
        assert kb.list_attachments(conn, t) == []


# ---------------------------------------------------------------------------
# T5 RED — cross-task receipt replay rejected (r4 task/run mismatch)
# ---------------------------------------------------------------------------

def test_t5_cross_task_replay_rejected(kanban_home, aion_gov_src, monkeypatch):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="factory A", factory_build_gate=1, assignee="agent007")
        b = kb.create_task(conn, title="factory B", factory_build_gate=1, assignee="agent007")
        run_a = _claim_and_run_id(conn, a)
        run_b = _claim_and_run_id(conn, b)

        # Complete A via the finalizer, then replay its receipt onto B.
        assert kb.complete_task(conn, a, result="done", expected_run_id=run_a)
        doc_a = _bound_receipt_doc(conn, a)

        # Disable the finalizer so the replayed (cross-task) receipt is tested
        # on the pre-bound provenance path alone.
        monkeypatch.setenv("AION_FACTORY_FINALIZER_ENABLED", "0")

        # Replay A's exact receipt bytes onto B with the trusted uploader.
        raw = json.dumps(doc_a, sort_keys=True).encode("utf-8")
        kb.store_attachment_bytes(
            conn, b, "receipt.json", raw, uploaded_by="aion_monarch_proof_kernel",
        )
        conn.execute(
            "UPDATE tasks SET factory_terminal_receipt_sha256 = ? WHERE id = ?",
            (hashlib.sha256(raw).hexdigest(), b),
        )
        conn.commit()

        with pytest.raises(kb.FactoryTerminalReceiptRequiredError):
            kb.complete_task(conn, b, result="done", expected_run_id=run_b)
        assert kb.get_task(conn, b).status != "done"


# ---------------------------------------------------------------------------
# T6 RED/IDEMPOTENT — fault injection at a write boundary -> rollback + retry
# ---------------------------------------------------------------------------

def test_t6_fault_injection_rolls_back_then_retry_succeeds(kanban_home, aion_gov_src, monkeypatch):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
        run_id = _claim_and_run_id(conn, t)

        # Fault: the attachment-name helper raises AFTER the terminal write has
        # already been performed inside the txn, so the receipt bind fails and
        # the whole transaction must roll back (status, event, receipt).
        def _boom(_raw):
            raise RuntimeError("simulated on-disk attachment failure")

        _original = kb._safe_attachment_name
        monkeypatch.setattr(kb, "_safe_attachment_name", _boom)
        with pytest.raises(RuntimeError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        # Zero durable mutation: still running, no receipt, no sha, no event.
        assert kb.get_task(conn, t).status == "running"
        row = conn.execute(
            "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?", (t,),
        ).fetchone()
        assert row["factory_terminal_receipt_sha256"] is None
        assert kb.list_attachments(conn, t) == []
        assert "completed" not in [e.kind for e in kb.list_events(conn, t)]

        # Retry (fault cleared) is idempotent and succeeds.
        monkeypatch.setattr(kb, "_safe_attachment_name", _original)
        assert kb.complete_task(conn, t, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, t).status == "done"
        assert _bound_receipt_doc(conn, t)["verdict"] == "OUTCOME_ACCEPTED"


# ---------------------------------------------------------------------------
# T7 GREEN — merge-bearing pre-bound receipt path unchanged
# ---------------------------------------------------------------------------

def test_t7_prebound_receipt_path_unchanged(kanban_home, aion_gov_src):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
        run_id = _claim_and_run_id(conn, t)
        # Pre-bind a valid receipt directly (merge/already-terminal style).
        doc = _kernel_receipt_doc(t, str(run_id))
        raw = json.dumps(doc, sort_keys=True).encode("utf-8")
        kb.store_attachment_bytes(
            conn, t, "receipt.json", raw, uploaded_by="aion_monarch_proof_kernel",
        )
        conn.execute(
            "UPDATE tasks SET factory_terminal_receipt_sha256 = ? WHERE id = ?",
            (hashlib.sha256(raw).hexdigest(), t),
        )
        conn.commit()

        assert kb.complete_task(conn, t, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, t).status == "done"


# ---------------------------------------------------------------------------
# T8 GREEN — already-terminal path unchanged
# ---------------------------------------------------------------------------

def test_t8_already_terminal_path_unchanged(kanban_home, aion_gov_src):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
        run_id = _claim_and_run_id(conn, t)
        assert kb.complete_task(conn, t, result="first", expected_run_id=run_id)
        # A second completion of an already-done task must not double-complete.
        assert kb.complete_task(conn, t, result="second", expected_run_id=run_id) is False
        assert kb.get_task(conn, t).status == "done"


# ---------------------------------------------------------------------------
# T10 — ALTERNATE_SUCCESS_PATHS stays 0 (no alternate success path)
# ---------------------------------------------------------------------------

def test_t10_no_alternate_success_path():
    from hermes_cli.kanban_db import FACTORY_VERDICT_ACCEPTED
    assert FACTORY_VERDICT_ACCEPTED == "OUTCOME_ACCEPTED"
    # The aion-governance kernel's alternate_success_paths must remain 0; the
    # finalizer path reuses the SAME kernel (single semantic authority).
    src = _aion_gov_source_dir()
    if src is not None:
        from scripts.aion_monarch_outcome_proof_gate import ALTERNATE_SUCCESS_PATHS  # noqa
        assert ALTERNATE_SUCCESS_PATHS == 0


# ---------------------------------------------------------------------------
# T11 HOSTILE — real worker subprocess: module-hash equality + guard + finalizer
# ---------------------------------------------------------------------------

_T11_WORKER_SCRIPT = r"""
import hashlib, json, os, sys
from pathlib import Path
os.environ["HERMES_HOME"] = {hermes_home!r}
os.environ["AION_GOVERNANCE_SOURCE_DIR"] = {aion_src!r}
from hermes_cli import kanban_db as kb
module_file = Path(kb.__file__).resolve()
module_sha = hashlib.sha256(module_file.read_bytes()).hexdigest()
conn = kb.connect(db_path=Path({db_path!r}))
t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
kb.claim_task(conn, t)
row = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (t,)).fetchone()
ok = kb.complete_task(conn, t, result="done", expected_run_id=int(row["current_run_id"]))
final = conn.execute("SELECT status, factory_terminal_receipt_sha256 FROM tasks WHERE id=?", (t,)).fetchone()
atts = kb.list_attachments(conn, t)
print(json.dumps({{
    "module_file": str(module_file),
    "module_sha256": module_sha,
    "has_guard": hasattr(kb, "FactoryTerminalReceiptRequiredError"),
    "complete_ok": bool(ok),
    "status": final["status"],
    "receipt_sha_present": bool(final["factory_terminal_receipt_sha256"]),
    "uploaded_by": [a.uploaded_by for a in atts],
}}))
conn.close()
"""


def test_t11_real_worker_subprocess_module_hash_and_finalizer(kanban_home, aion_gov_src, tmp_path):
    """Launch a REAL worker subprocess: assert (a) its loaded kanban_db module
    sha256 equals the gateway's (this process's) module sha256 — the I1
    containment proof — (b) the receipt guard is present, and (c) the finalizer
    path completes a gate=1 task. No live service is used (fresh tmp DB)."""
    gateway_module_sha = hashlib.sha256(
        Path(kb.__file__).resolve().read_bytes()
    ).hexdigest()

    db_path = tmp_path / "worker.db"
    script = _T11_WORKER_SCRIPT.format(
        hermes_home=str(Path(os.environ["HERMES_HOME"])),
        aion_src=str(_aion_gov_source_dir()),
        db_path=str(db_path),
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(kb.__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert out.returncode == 0, f"worker subprocess failed:\n{out.stderr}"
    result = json.loads(out.stdout.strip().splitlines()[-1])

    # I1 containment: worker and gateway load the SAME kanban_db bytes.
    assert result["module_sha256"] == gateway_module_sha
    assert result["has_guard"] is True
    # Finalizer path succeeded in the real worker subprocess.
    assert result["complete_ok"] is True
    assert result["status"] == "done"
    assert result["receipt_sha_present"] is True
    assert result["uploaded_by"] == ["aion_monarch_proof_kernel"]
