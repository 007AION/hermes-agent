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
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


_PR917_AUTHORITY_COMMIT = "15e6c82f4020c53cdba511c1e7ca31bab1bfe6bb"
_PR917_MODULES = {
    "scripts/aion_monarch_outcome_proof_gate.py": (
        "402d7882786093a96826601bfa443fa24efa681b18d94f7d3e8ed1d0cc4d32dc"
    ),
    "scripts/aion_monarch_typed_adapters.py": (
        "fc36d5b6d9b0edf1148ab99e2288f02b960abb3a9ebfd6ea2ef0d4b7b494b092"
    ),
    "scripts/aion_monarch_receipt_binder.py": (
        "5c8e6b517a390fd2d826d464036fc4e4da3f8ed4a9d1d0313f809bac3b1682be"
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + fully rebound Native Kanban env pins.

    Rebinds ``HERMES_KANBAN_*`` path pins and clears the board/worker pins via
    :func:`kanban_db.isolated_kanban_env` so ``connect()`` and the
    attachment/event/board paths can never resolve to the live aion-factory
    board (the AION-RL2-CORE-01-R10 synthetic-residue class). Setting only
    ``HERMES_HOME`` is insufficient: the dispatcher injects
    ``HERMES_KANBAN_HOME`` / ``HERMES_KANBAN_BOARD`` with higher precedence.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.isolated_kanban_env(tmp_path):
        kb.init_db()
        yield home


def _materialize_pr917_git_object(tmp_path: Path) -> Path | None:
    """Materialize PR #917's immutable merge object without reading its worktree."""
    repo = Path(os.environ.get("HOME", "")) / "aion-governance"
    if not repo.is_dir():
        return None
    dest = tmp_path / _PR917_AUTHORITY_COMMIT
    for rel_path, expected_sha in _PR917_MODULES.items():
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{_PR917_AUTHORITY_COMMIT}:{rel_path}"],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0 or hashlib.sha256(proc.stdout).hexdigest() != expected_sha:
            return None
        target = dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(proc.stdout)
    return dest


def _aion_gov_source_dir(tmp_path: Path | None = None) -> Path | None:
    """Resolve only a byte-exact PR #917 authority source for integration tests."""
    if tmp_path is not None:
        immutable = _materialize_pr917_git_object(tmp_path)
        if immutable is not None:
            return immutable
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
def aion_gov_src(monkeypatch, tmp_path):
    """Point the finalizer at immutable PR #917 bytes, or skip if unavailable."""
    src = _aion_gov_source_dir(tmp_path)
    if src is None:
        pytest.skip("AION_GOVERNANCE_SOURCE_DIR not configured")
    monkeypatch.setenv("AION_GOVERNANCE_SOURCE_DIR", str(src))
    for rel_path, expected_sha in _PR917_MODULES.items():
        module_bytes = (src / rel_path).read_bytes()
        if hashlib.sha256(module_bytes).hexdigest() != expected_sha:
            pytest.skip(f"aion-governance module {rel_path} does not match PR #917")
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
# PR #917 immutable authority and source-drift fence
# ---------------------------------------------------------------------------

def test_pr917_authority_is_exact_and_mutable_checkout_is_not_a_default():
    assert kb.AION_GOVERNANCE_AUTHORITY_PR == 917
    assert kb.AION_GOVERNANCE_AUTHORITY_HEAD == (
        "44d4c221468d4035e078a6bfbcd4e8a25de4850a"
    )
    assert kb.AION_GOVERNANCE_AUTHORITY_COMMIT == _PR917_AUTHORITY_COMMIT
    assert kb.AION_GOVERNANCE_KERNEL_SHA256 == _PR917_MODULES[
        "scripts/aion_monarch_outcome_proof_gate.py"
    ]
    assert kb.AION_GOVERNANCE_TYPED_ADAPTERS_SHA256 == _PR917_MODULES[
        "scripts/aion_monarch_typed_adapters.py"
    ]
    assert kb.AION_GOVERNANCE_RECEIPT_BINDER_SHA256 == _PR917_MODULES[
        "scripts/aion_monarch_receipt_binder.py"
    ]
    assert all(
        _PR917_AUTHORITY_COMMIT in source_dir
        for source_dir in kb.AION_GOVERNANCE_DEFAULT_SOURCE_DIRS
    )
    assert "/root/aion-governance" not in kb.AION_GOVERNANCE_DEFAULT_SOURCE_DIRS


def test_pr917_module_drift_fails_closed_with_zero_mutation(
    kanban_home, aion_gov_src, tmp_path, monkeypatch,
):
    drifted = tmp_path / "drifted-pr917"
    for rel_path in _PR917_MODULES:
        target = drifted / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((aion_gov_src / rel_path).read_bytes())
    binder_path = drifted / "scripts" / "aion_monarch_receipt_binder.py"
    binder_path.write_bytes(binder_path.read_bytes() + b"\n# injected drift\n")
    monkeypatch.setenv("AION_GOVERNANCE_SOURCE_DIR", str(drifted))

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="drift-fenced", factory_build_gate=1, assignee="agent007",
        )
        run_id = _claim_and_run_id(conn, task_id)
        status_before = kb.get_task(conn, task_id).status
        events_before = [event.kind for event in kb.list_events(conn, task_id)]

        with pytest.raises(
            kb.FactoryTerminalReceiptRequiredError,
            match="aion_monarch_receipt_binder.py sha256 mismatch",
        ):
            kb.complete_task(conn, task_id, result="must roll back", expected_run_id=run_id)

        row = conn.execute(
            "SELECT status, factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == status_before == "running"
        assert row["factory_terminal_receipt_sha256"] is None
        assert kb.list_attachments(conn, task_id) == []
        assert [event.kind for event in kb.list_events(conn, task_id)] == events_before
        assert conn.execute(
            "SELECT COUNT(*) FROM factory_terminal_write_grants"
        ).fetchone()[0] == 0


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


def _reviewed_author_chain(conn, *, canonical_merger_receipt=False, source_pr=49):
    """Create one exact reviewed author -> auditor -> merger -> runtime chain."""
    head = (
        "d622fcf38da613ce25bf4eaf37c54a94053d5e70"
        if canonical_merger_receipt else "1" * 40
    )
    tree = (
        "1e5e822f33c5fe227982ff9e4e1de320cb862c42"
        if canonical_merger_receipt else "2" * 40
    )
    base = (
        "f6dfcb6d50f512e0944cd8f56367ccb0eac6ace8"
        if canonical_merger_receipt else "3" * 40
    )
    merge_sha = (
        "9920fd369b4b0c14bc292b7585431f16a4028f31"
        if canonical_merger_receipt else "4" * 40
    )
    review_id = 5037761833 if canonical_merger_receipt else 12345
    changed_files = (
        ["hermes_cli/kanban_db.py", "tests/hermes_cli/test_kanban_factory_finalizer.py"]
        if canonical_merger_receipt else ["hermes_cli/kanban_db.py"]
    )
    author = kb.create_task(
        conn, title="reviewed author", factory_build_gate=1, assignee="agent007",
    )
    reviewer = kb.create_task(
        conn, title="exact-head audit", factory_build_gate=1,
        assignee="bafuxunan", parents=[author],
    )
    merger = kb.create_task(
        conn, title="role-separated merge", factory_build_gate=1,
        assignee="merger", parents=[reviewer],
    )
    runtime = kb.create_task(
        conn, title="runtime readback", factory_build_gate=1,
        assignee="gm", parents=[merger],
    )

    author_run = _claim_and_run_id(conn, author)
    handoff = kb.request_review_handoff(
        conn,
        author,
        expected_run_id=author_run,
        review_task_id=reviewer,
        reason=f"PR #{source_pr} frozen at exact head {head}",
    )
    assert handoff is not None

    reviewer_run = _claim_and_run_id(conn, reviewer)
    review_reason = (
        f"APPROVE_EXACT_HEAD head={head} tree={tree} review={review_id}"
    )
    assert kb.record_review_verdict(
        conn,
        author,
        review_task_id=reviewer,
        expected_review_run_id=reviewer_run,
        verdict="pass",
        reason=review_reason,
    )
    assert kb.complete_task(
        conn,
        reviewer,
        expected_run_id=reviewer_run,
        summary="independent exact-head audit passed",
        metadata={
            "author_task": author,
            "review_outcome": "APPROVE_EXACT_HEAD",
            "head_sha": head,
            "tree_sha": tree,
            "base_sha": base,
            "github_review_id": review_id,
            "changed_files": changed_files,
        },
    )

    merger_run = _claim_and_run_id(conn, merger)
    merger_metadata = {
        "repository": "kiddhu/hermes-agent",
        "pr_number": source_pr,
        "head_sha": head,
        "tree_sha": tree,
        "audited_base_sha": base,
        "merge_commit_sha": merge_sha,
        "merged_by": "kiddhu",
        "review_id": review_id,
        "author": "007AION",
        "auditor": "GemAION",
        "role_separation": {
            "author": "007AION",
            "auditor": "GemAION",
            "merger": "kiddhu",
            "distinct": True,
        },
        "forbidden_actions_performed": [],
        "secret_exposure": "none",
    }
    if canonical_merger_receipt:
        merger_metadata = {
            "verdict": "EXACT_HEAD_MERGED_MAIN_READBACK",
            "native_profile": "merger",
            "native_task_id": merger,
            "native_run_id": merger_run,
            "repository": "kiddhu/hermes-agent",
            "pr_number": source_pr,
            "expected_head": head,
            "audited_tree": tree,
            "audited_base": base,
            "merge_commit_sha": merge_sha,
            "merged_by": "kiddhu",
            "canonical_main_sha": merge_sha,
            "canonical_main_parents": ["6" * 40, head],
            "audited_head_is_main_parent": True,
            "main_equals_merge_commit": True,
            "implementation_task_id": author,
            "implementation_run_id": author_run,
            "implementation_profile": "agent007",
            "implementation_actor": "007AION",
            "audit_task_id": reviewer,
            "audit_run_id": reviewer_run,
            "audit_profile": "bafuxunan",
            "auditor_actor": "GemAION",
            "github_review_id": review_id,
            "gate_verdict": "PASS",
            "merge_performed": True,
            "production_or_runtime_mutation": False,
        }
    assert kb.complete_task(
        conn,
        merger,
        expected_run_id=merger_run,
        summary="role-separated merge readback passed",
        metadata=merger_metadata,
    )

    runtime_run = _claim_and_run_id(conn, runtime)
    assert kb.complete_task(
        conn,
        runtime,
        expected_run_id=runtime_run,
        summary="exact runtime installed and read back",
        metadata={
            "canonical_run_id": runtime_run,
            "install": {
                "head": merge_sha,
                "tree": tree,
                "changed_paths": changed_files,
            },
            "forbidden_actions_performed": [],
            "secret_exposure": "none",
        },
    )
    return author, author_run, reviewer, merger, runtime


def test_reviewed_author_accepts_canonical_immutable_merger_receipt(
    kanban_home, aion_gov_src,
):
    """The real merger lane's immutable canonical schema authenticates exactly."""
    with kb.connect() as conn:
        author, author_run, *_ = _reviewed_author_chain(
            conn, canonical_merger_receipt=True, source_pr=50,
        )

        assert kb._reviewed_author_finalizer_run_id(conn, author) == author_run
        assert kb.complete_task(conn, author, summary="canonical chain terminalized")
        assert kb.get_task(conn, author).status == "done"


@pytest.mark.parametrize(
    ("drift", "mutate"),
    [
        ("stale_head", lambda md: md.__setitem__("expected_head", "9" * 40)),
        ("stale_tree", lambda md: md.__setitem__("audited_tree", "9" * 40)),
        ("stale_base", lambda md: md.__setitem__("audited_base", "9" * 40)),
        ("mixed_schema", lambda md: md.__setitem__("head_sha", md["expected_head"])),
        ("partial_schema", lambda md: md.pop("audited_base")),
        ("implementation_task", lambda md: md.__setitem__("implementation_task_id", "t_forged")),
        ("implementation_run", lambda md: md.__setitem__("implementation_run_id", -1)),
        ("implementation_profile", lambda md: md.__setitem__("implementation_profile", "intruder")),
        ("implementation_actor", lambda md: md.__setitem__("implementation_actor", "GemAION")),
        ("audit_task", lambda md: md.__setitem__("audit_task_id", "t_forged")),
        ("audit_run", lambda md: md.__setitem__("audit_run_id", -1)),
        ("audit_profile", lambda md: md.__setitem__("audit_profile", "intruder")),
        ("auditor_actor", lambda md: md.__setitem__("auditor_actor", "007AION")),
        ("native_task", lambda md: md.__setitem__("native_task_id", "t_forged")),
        ("native_run", lambda md: md.__setitem__("native_run_id", -1)),
        ("native_profile", lambda md: md.__setitem__("native_profile", "gm")),
        ("repository", lambda md: md.__setitem__("repository", "attacker/repo")),
        ("pr_number", lambda md: md.__setitem__("pr_number", 999)),
        ("review", lambda md: md.__setitem__("github_review_id", 99999)),
        ("canonical_main", lambda md: md.__setitem__("canonical_main_sha", "9" * 40)),
        ("main_parent", lambda md: md.__setitem__("canonical_main_parents", ["6" * 40])),
        ("gate_verdict", lambda md: md.__setitem__("gate_verdict", "FAIL")),
        ("merge_performed", lambda md: md.__setitem__("merge_performed", False)),
        (
            "runtime_mutation",
            lambda md: md.__setitem__("production_or_runtime_mutation", True),
        ),
    ],
)
def test_canonical_merger_receipt_drift_fails_closed_zero_author_mutation(
    kanban_home, aion_gov_src, drift, mutate,
):
    with kb.connect() as conn:
        author, _author_run, _reviewer, merger, _runtime = _reviewed_author_chain(
            conn, canonical_merger_receipt=True, source_pr=50,
        )
        _rewrite_latest_run_metadata(conn, merger, mutate)
        before_task = dict(conn.execute(
            "SELECT status, current_run_id, completed_at, "
            "factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (author,),
        ).fetchone())
        before_runs = [tuple(row) for row in conn.execute(
            "SELECT id, status, outcome, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id",
            (author,),
        ).fetchall()]
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0]

        with pytest.raises(kb.FactoryTerminalReceiptRequiredError):
            kb.complete_task(conn, author, summary=f"reject {drift}")

        assert dict(conn.execute(
            "SELECT status, current_run_id, completed_at, "
            "factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (author,),
        ).fetchone()) == before_task
        assert [tuple(row) for row in conn.execute(
            "SELECT id, status, outcome, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id",
            (author,),
        ).fetchall()] == before_runs
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0] == before_events
        assert kb.list_attachments(conn, author) == []


def test_reviewed_author_controller_completion_uses_exact_ended_run(
    kanban_home, aion_gov_src,
):
    """RED: reviewed authors currently cannot enter the trusted finalizer."""
    with kb.connect() as conn:
        author, author_run, *_ = _reviewed_author_chain(conn)
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (author,),
        ).fetchone()[0]

        assert kb.complete_task(
            conn,
            author,
            summary="reviewed candidate terminalized",
        )
        assert kb.get_task(conn, author).status == "done"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (author,),
        ).fetchone()[0] == run_count
        completed = conn.execute(
            "SELECT run_id FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (author,),
        ).fetchone()
        assert completed is not None and completed["run_id"] == author_run


def _rewrite_latest_run_metadata(conn, task_id, mutate):
    row = conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    metadata = json.loads(row["metadata"])
    mutate(metadata)
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (json.dumps(metadata), row["id"]),
    )
    conn.commit()


@pytest.mark.parametrize(
    "drift",
    [
        "stale_author_run",
        "active_author_run",
        "wrong_reviewer_identity",
        "nonmatching_reviewer_identity",
        "nonmatching_merger_identity",
        "multiple_reviewers",
        "review_head",
        "merge_review",
        "merge_actor",
        "merge_repository",
        "merge_pr_number",
        "runtime_tree",
        "runtime_receipt_authenticator",
    ],
)
def test_reviewed_author_evidence_drift_fails_closed_zero_mutation(
    kanban_home, aion_gov_src, drift,
):
    with kb.connect() as conn:
        author, author_run, reviewer, merger, runtime = _reviewed_author_chain(conn)

        if drift == "stale_author_run":
            conn.execute(
                "UPDATE task_runs SET outcome = 'completed' WHERE id = ?",
                (author_run,),
            )
            conn.commit()
        elif drift == "active_author_run":
            conn.execute(
                "UPDATE task_runs SET ended_at = NULL WHERE id = ?", (author_run,),
            )
            conn.commit()
        elif drift == "wrong_reviewer_identity":
            conn.execute(
                "UPDATE task_runs SET profile = 'agent007' WHERE task_id = ?",
                (reviewer,),
            )
            conn.commit()
        elif drift == "nonmatching_reviewer_identity":
            conn.execute(
                "UPDATE tasks SET assignee = 'intruder-reviewer' WHERE id = ?",
                (reviewer,),
            )
            conn.execute(
                "UPDATE task_runs SET profile = 'intruder-reviewer' WHERE task_id = ?",
                (reviewer,),
            )
            conn.commit()
        elif drift == "nonmatching_merger_identity":
            conn.execute(
                "UPDATE tasks SET assignee = 'intruder-merger' WHERE id = ?",
                (merger,),
            )
            conn.execute(
                "UPDATE task_runs SET profile = 'intruder-merger' WHERE task_id = ?",
                (merger,),
            )
            conn.commit()
        elif drift == "multiple_reviewers":
            event = conn.execute(
                "SELECT run_id, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'review_verdict'",
                (author,),
            ).fetchone()
            conn.execute(
                "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'review_verdict', ?, ?)",
                (author, event["run_id"], event["payload"], int(time.time())),
            )
            conn.commit()
        elif drift == "review_head":
            _rewrite_latest_run_metadata(
                conn, reviewer, lambda md: md.__setitem__("head_sha", "9" * 40),
            )
        elif drift == "merge_review":
            _rewrite_latest_run_metadata(
                conn, merger, lambda md: md.__setitem__("review_id", 99999),
            )
        elif drift == "merge_actor":
            _rewrite_latest_run_metadata(
                conn, merger, lambda md: md.__setitem__("merged_by", "attacker"),
            )
        elif drift == "merge_repository":
            _rewrite_latest_run_metadata(
                conn, merger,
                lambda md: md.__setitem__("repository", "attacker/other-repo"),
            )
        elif drift == "merge_pr_number":
            _rewrite_latest_run_metadata(
                conn, merger, lambda md: md.__setitem__("pr_number", 999),
            )
        elif drift == "runtime_tree":
            _rewrite_latest_run_metadata(
                conn, runtime,
                lambda md: md["install"].__setitem__("tree", "9" * 40),
            )
        elif drift == "runtime_receipt_authenticator":
            conn.execute(
                "UPDATE task_attachments SET uploaded_by = 'agent' WHERE task_id = ?",
                (runtime,),
            )
            conn.commit()

        before_task = dict(conn.execute(
            "SELECT status, current_run_id, completed_at, "
            "factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (author,),
        ).fetchone())
        before_runs = [tuple(row) for row in conn.execute(
            "SELECT id, status, outcome, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id",
            (author,),
        ).fetchall()]
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0]

        with pytest.raises(kb.FactoryTerminalReceiptRequiredError):
            kb.complete_task(conn, author, summary="must not terminalize")

        assert dict(conn.execute(
            "SELECT status, current_run_id, completed_at, "
            "factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (author,),
        ).fetchone()) == before_task
        assert [tuple(row) for row in conn.execute(
            "SELECT id, status, outcome, ended_at FROM task_runs "
            "WHERE task_id = ? ORDER BY id",
            (author,),
        ).fetchall()] == before_runs
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0] == before_events
        assert kb.list_attachments(conn, author) == []


@pytest.mark.parametrize("sabotage", ["false_verdict", "signer_failure", "cas_miss"])
def test_reviewed_author_finalizer_sabotage_rolls_back_zero_mutation(
    kanban_home, aion_gov_src, monkeypatch, sabotage,
):
    with kb.connect() as conn:
        author, author_run, *_ = _reviewed_author_chain(conn)
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0]
        before_run = tuple(conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (author_run,),
        ).fetchone())

        if sabotage in {"false_verdict", "signer_failure"}:
            real_binder, real_kernel, real_adapters = kb._load_pinned_aion_modules(
                author
            )

            class _SabotagedBinder:
                def bind_task_terminal_in_txn(self, **kwargs):
                    kwargs["terminal_write"](
                        kwargs["conn"], kwargs["task_id"], kwargs["run_id"]
                    )
                    if sabotage == "signer_failure":
                        raise RuntimeError("simulated signer/authenticator failure")
                    return {
                        "bound": False,
                        "verdict": "FAIL_CLOSED",
                        "failed_conditions": ["C8"],
                    }

            monkeypatch.setattr(
                kb,
                "_load_pinned_aion_modules",
                lambda _task_id: (_SabotagedBinder(), real_kernel, real_adapters),
            )
        else:
            real_resolver = getattr(kb, "_reviewed_author_finalizer_run_id")
            calls = {"count": 0}

            def _cas_miss(c, task_id):
                calls["count"] += 1
                if calls["count"] == 1:
                    return real_resolver(c, task_id)
                return None

            monkeypatch.setattr(kb, "_reviewed_author_finalizer_run_id", _cas_miss)

        with pytest.raises((kb.FactoryTerminalReceiptRequiredError, RuntimeError)):
            kb.complete_task(conn, author, summary="must roll back")

        row = conn.execute(
            "SELECT status, current_run_id, completed_at, "
            "factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (author,),
        ).fetchone()
        assert tuple(row) == ("review", None, None, None)
        assert tuple(conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (author_run,),
        ).fetchone()) == before_run
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0] == before_events
        assert kb.list_attachments(conn, author) == []
        assert _receipt_residue_on_disk(conn, author) == []


def test_reviewed_author_prebound_receipt_cannot_bypass_evidence_drift(
    kanban_home, aion_gov_src,
):
    with kb.connect() as conn:
        author, author_run, _reviewer, _merger, runtime = _reviewed_author_chain(conn)
        _rewrite_latest_run_metadata(
            conn, runtime,
            lambda md: md["install"].__setitem__("head", "9" * 40),
        )
        receipt = _kernel_receipt_doc(author, str(author_run))
        raw = json.dumps(receipt, sort_keys=True).encode("utf-8")
        kb.store_attachment_bytes(
            conn,
            author,
            "prebound.json",
            raw,
            uploaded_by="aion_monarch_proof_kernel",
        )
        conn.execute(
            "UPDATE tasks SET factory_terminal_receipt_sha256 = ? WHERE id = ?",
            (hashlib.sha256(raw).hexdigest(), author),
        )
        conn.commit()
        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0]

        with pytest.raises(kb.FactoryTerminalReceiptRequiredError):
            kb.complete_task(conn, author, summary="must not bypass evidence")

        row = conn.execute(
            "SELECT status, current_run_id, completed_at FROM tasks WHERE id = ?",
            (author,),
        ).fetchone()
        assert tuple(row) == ("review", None, None)
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (author,),
        ).fetchone()[0] == before_events


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
# F2 repair: bind to the audited CANDIDATE bytes despite editable/user-site
# import hooks. Strip every editable MetaPathFinder (setuptools _EditableFinder
# et al.) so it cannot shadow the candidate package, then pin the candidate
# repo to the head of sys.path BEFORE importing hermes_cli.kanban_db.
sys.meta_path[:] = [
    f for f in sys.meta_path
    if "_Editable" not in repr(f) and "Editable" not in f.__class__.__name__
]
sys.path.insert(0, {candidate_repo!r})
from hermes_cli import kanban_db as kb
module_file = Path(kb.__file__).resolve()
module_sha = hashlib.sha256(module_file.read_bytes()).hexdigest()
candidate_file = Path({candidate_file!r}).resolve()
path_matches_candidate = (module_file == candidate_file)
# F2 containment: fully isolate the Native Kanban env so connect()/attachments/
# events/claims resolve to the isolated root, never the live aion-factory board.
with kb.isolated_kanban_env(Path({worker_home!r})):
    conn = kb.connect(db_path=Path({db_path!r}))
    t = kb.create_task(conn, title="factory task", factory_build_gate=1, assignee="agent007")
    kb.claim_task(conn, t)
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (t,)).fetchone()
    ok = kb.complete_task(conn, t, result="done", expected_run_id=int(row["current_run_id"]))
    final = conn.execute("SELECT status, factory_terminal_receipt_sha256 FROM tasks WHERE id=?", (t,)).fetchone()
    atts = kb.list_attachments(conn, t)
    conn.close()
print(json.dumps({{
    "module_file": str(module_file),
    "candidate_file": str(candidate_file),
    "path_matches_candidate": bool(path_matches_candidate),
    "module_sha256": module_sha,
    "has_guard": hasattr(kb, "FactoryTerminalReceiptRequiredError"),
    "complete_ok": bool(ok),
    "status": final["status"],
    "receipt_sha_present": bool(final["factory_terminal_receipt_sha256"]),
    "uploaded_by": [a.uploaded_by for a in atts],
}}))
"""


def test_t11_real_worker_subprocess_module_hash_and_finalizer(kanban_home, aion_gov_src, tmp_path):
    """Launch a REAL worker subprocess: assert (a) its loaded kanban_db module
    sha256 equals the gateway's (this process's) module sha256 — the I1
    containment proof — (b) the receipt guard is present, and (c) the finalizer
    path completes a gate=1 task. The child is pinned to the audited CANDIDATE
    bytes despite editable/user-site import hooks (F2 repair). No live service
    is used (fresh tmp DB)."""
    candidate_file = Path(kb.__file__).resolve()
    candidate_repo = candidate_file.parents[1]  # the hermes-agent checkout root
    gateway_module_sha = hashlib.sha256(candidate_file.read_bytes()).hexdigest()

    db_path = tmp_path / "worker.db"
    worker_home = tmp_path / "worker_home"
    script = _T11_WORKER_SCRIPT.format(
        hermes_home=str(Path(os.environ["HERMES_HOME"])),
        aion_src=str(_aion_gov_source_dir()),
        db_path=str(db_path),
        worker_home=str(worker_home),
        candidate_repo=str(candidate_repo),
        candidate_file=str(candidate_file),
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(candidate_repo) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert out.returncode == 0, f"worker subprocess failed:\n{out.stderr}"
    result = json.loads(out.stdout.strip().splitlines()[-1])

    # F2 repair: the child must have loaded the exact CANDIDATE bytes despite
    # editable/user-site import hooks — assert path AND module hash equality
    # BEFORE trusting the behavioral results.
    assert result["path_matches_candidate"] is True, (
        f"child imported {result['module_file']!r}, expected candidate "
        f"{result['candidate_file']!r} (editable/user-site hook not neutralised)"
    )
    # I1 containment: worker and gateway load the SAME kanban_db bytes.
    assert result["module_sha256"] == gateway_module_sha
    assert result["has_guard"] is True
    # Finalizer path succeeded in the real worker subprocess.
    assert result["complete_ok"] is True
    assert result["status"] == "done"
    assert result["receipt_sha_present"] is True
    assert result["uploaded_by"] == ["aion_monarch_proof_kernel"]


# ---------------------------------------------------------------------------
# F3 — complete fault matrix: every transaction/file boundary + idempotent retry
# ---------------------------------------------------------------------------
# The R1 audit found only a pre-file-write fault was covered (T6); the omitted
# post-file boundary (fault AFTER receipt file + DB attachment INSERT, BEFORE
# receipt-sha update) left an orphan receipt file on SQLite rollback. These
# tests close that gap: each boundary is faulted, the transaction rolls back to
# zero mutation with NO on-disk residue, and a deterministic retry succeeds.
# Boundaries: M1 terminal write (status CAS), M2 before file write,
# M3 after file+DB insert before sha (the F1 boundary), M4 after DB insert
# before the 'attached' event, M5 after sha before commit.


def _receipt_residue_on_disk(conn, task_id) -> list[str]:
    """Receipt + staging files under the task's attachments dir (must be empty)."""
    att_dir = kb.task_attachments_dir(task_id)
    if not att_dir.exists():
        return []
    out = []
    for p in att_dir.rglob("*"):
        if p.is_file() and (
            "aion_monarch_receipt" in p.name or ".staging." in p.name
        ):
            out.append(str(p))
    return out


def _assert_zero_mutation_no_residue(conn, task_id, run_id) -> None:
    assert kb.get_task(conn, task_id).status == "running"
    row = conn.execute(
        "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["factory_terminal_receipt_sha256"] is None
    assert kb.list_attachments(conn, task_id) == []
    kinds = [e.kind for e in kb.list_events(conn, task_id)]
    assert "attached" not in kinds
    assert "completed" not in kinds
    # F1: no orphaned receipt or staging file may survive the rollback.
    assert _receipt_residue_on_disk(conn, task_id) == []


def _install_one_shot_binder_fault(monkeypatch, fault_kind):
    """Monkeypatch the finalizer's pinned-module loader so the REAL binder
    raises once at the named boundary, then delegates normally on retry."""
    real_binder, real_kernel, real_adapters = kb._load_pinned_aion_modules("__fault__")
    state = {"fired": False}

    class _FaultyBinder:
        def bind_task_terminal_in_txn(self, **kwargs):
            if not state["fired"]:
                state["fired"] = True
                if fault_kind == "terminal_write":
                    def _boom(*a, **k):
                        raise RuntimeError("FAULT_AT_TERMINAL_WRITE")
                    kwargs["terminal_write"] = _boom
                elif fault_kind == "before_file":
                    def _boom(*a, **k):
                        raise RuntimeError("FAULT_BEFORE_FILE_WRITE")
                    kwargs["store_attachment"] = _boom
                elif fault_kind == "before_sha":
                    def _boom(conn, task_id, sha):
                        raise RuntimeError(
                            "FAULT_AFTER_FILE_AND_DB_INSERT_BEFORE_SHA"
                        )
                    kwargs["set_factory_terminal_receipt_sha"] = _boom
                elif fault_kind == "after_sha":
                    real_binder.bind_task_terminal_in_txn(**kwargs)
                    raise RuntimeError("FAULT_AFTER_SHA_BEFORE_COMMIT")
            return real_binder.bind_task_terminal_in_txn(**kwargs)

    monkeypatch.setattr(
        kb, "_load_pinned_aion_modules",
        lambda task_id: (_FaultyBinder(), real_kernel, real_adapters),
    )


@pytest.mark.parametrize(
    "fault_kind",
    ["terminal_write", "before_file", "before_sha", "after_sha"],
)
def test_f3_binder_boundary_fault_rolls_back_no_residue_then_retries(
    kanban_home, aion_gov_src, monkeypatch, fault_kind,
):
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="factory task", factory_build_gate=1, assignee="agent007",
        )
        run_id = _claim_and_run_id(conn, t)

        _install_one_shot_binder_fault(monkeypatch, fault_kind)
        with pytest.raises(RuntimeError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        _assert_zero_mutation_no_residue(conn, t, run_id)

        # Deterministic idempotent retry (fault already fired once) succeeds.
        assert kb.complete_task(conn, t, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, t).status == "done"
        assert _bound_receipt_doc(conn, t)["verdict"] == "OUTCOME_ACCEPTED"
        att_dir = kb.task_attachments_dir(t)
        receipt_files = [
            p for p in att_dir.rglob("aion_monarch_receipt*.json") if p.is_file()
        ]
        assert len(receipt_files) == 1
        assert [p for p in att_dir.rglob("*.staging.*")] == []


def test_f3_fault_after_db_insert_before_event_no_residue_then_retries(
    kanban_home, aion_gov_src, monkeypatch,
):
    """M4: fault after the attachment row INSERT but before the 'attached' event.

    The finalizer's _store_attachment writes the staged file, INSERTs the row,
    then emits the 'attached' event. Faulting _append_event for that one call
    proves the staged file is discarded and no row survives the rollback."""
    orig_append = kb._append_event
    state = {"fired": False}

    def _one_shot_append(conn, task_id, kind, payload=None, **kwargs):
        if kind == "attached" and not state["fired"]:
            state["fired"] = True
            raise RuntimeError("FAULT_AFTER_DB_INSERT_BEFORE_EVENT")
        return orig_append(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", _one_shot_append)
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="factory task", factory_build_gate=1, assignee="agent007",
        )
        run_id = _claim_and_run_id(conn, t)

        with pytest.raises(RuntimeError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        _assert_zero_mutation_no_residue(conn, t, run_id)

        assert kb.complete_task(conn, t, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, t).status == "done"
        assert _bound_receipt_doc(conn, t)["verdict"] == "OUTCOME_ACCEPTED"
        att_dir = kb.task_attachments_dir(t)
        assert len([p for p in att_dir.rglob("aion_monarch_receipt*.json") if p.is_file()]) == 1


# ---------------------------------------------------------------------------
# F4 — post-COMMIT/pre-promote promotion failure + hard-crash/restart (R3)
# ---------------------------------------------------------------------------
# The R2 audit reproduced that promoting the staged receipt only AFTER SQLite
# COMMIT left authoritative done/sha/attachment/event state pointing at a
# missing receipt (os.replace injected to fail), and ordinary retry died on a
# terminal CAS miss. These tests prove the reorder: promotion now happens
# BEFORE COMMIT, so a promotion failure rolls back to zero mutation with no
# residue and a deterministic retry succeeds, and a hard crash in the narrow
# promote->commit window leaves only an unreachable orphan (no DB reference)
# that a restart recovers from cleanly.


def test_f4_promotion_failure_rolls_back_no_residue_then_retries(
    kanban_home, aion_gov_src, monkeypatch,
):
    """Promotion failure (os.replace raises on the staged file) must NOT commit
    terminal state; the txn rolls back to zero mutation and retry is clean."""
    real_replace = os.replace

    def _fail_staged(src, dst):
        if ".staging." in Path(src).name:
            raise OSError("INJECTED_POST_COMMIT_PROMOTE_FAILURE")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_staged)
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="factory task", factory_build_gate=1, assignee="agent007",
        )
        child = kb.create_task(conn, title="child", assignee="agent007", parents=[t])
        run_id = _claim_and_run_id(conn, t)

        with pytest.raises(OSError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        _assert_zero_mutation_no_residue(conn, t, run_id)
        # Child must not wake on a rolled-back completion.
        assert kb.get_task(conn, child).status == "todo"

        # Deterministic idempotent retry (fault cleared) succeeds.
        monkeypatch.setattr(os, "replace", real_replace)
        assert kb.complete_task(conn, t, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, t).status == "done"
        assert _bound_receipt_doc(conn, t)["verdict"] == "OUTCOME_ACCEPTED"
        assert kb.get_task(conn, child).status == "ready"
        att_dir = kb.task_attachments_dir(t)
        assert len([p for p in att_dir.rglob("aion_monarch_receipt*.json") if p.is_file()]) == 1
        assert [p for p in att_dir.rglob("*.staging.*")] == []


_CRASH_CHILD_SCRIPT = r"""
import os, sys
from pathlib import Path
sys.meta_path[:] = [
    f for f in sys.meta_path
    if "_Editable" not in repr(f) and "Editable" not in f.__class__.__name__
]
sys.path.insert(0, {candidate_repo!r})
os.environ["HERMES_HOME"] = {hermes_home!r}
os.environ["AION_GOVERNANCE_SOURCE_DIR"] = {aion_src!r}
from hermes_cli import kanban_db as kb
with kb.isolated_kanban_env(Path({worker_home!r})):
    kb.init_db()
    conn = kb.connect()
    t = kb.create_task(conn, title="crash-window-task", assignee="agent007", factory_build_gate=1)
    child = kb.create_task(conn, title="crash-child", assignee="agent007", parents=[t])
    kb.claim_task(conn, t)
    run_id = int(conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (t,)).fetchone()[0])
    real_replace = kb.os.replace
    def crash_after_promote(src, dst):
        if ".staging." in Path(src).name:
            real_replace(src, dst)  # promotion succeeds ...
            os._exit(99)            # ... then hard crash BEFORE COMMIT
        return real_replace(src, dst)
    kb.os.replace = crash_after_promote
    kb.complete_task(conn, t, result="done", expected_run_id=run_id)
    os._exit(0)
"""


def test_f4_hard_crash_restart_no_broken_terminal_state_recovers(
    kanban_home, aion_gov_src, tmp_path,
):
    """A hard crash (os._exit) after promotion but before COMMIT must leave the
    task running with no authoritative receipt/sha/attachment/event; only an
    unreachable orphan final file (no DB reference). A restart then completes
    deterministically and wakes the dependent child."""
    candidate_file = Path(kb.__file__).resolve()
    candidate_repo = candidate_file.parents[1]
    worker_home = tmp_path / "crash_home"
    script = _CRASH_CHILD_SCRIPT.format(
        candidate_repo=str(candidate_repo),
        hermes_home=str(Path(os.environ["HERMES_HOME"])),
        aion_src=str(_aion_gov_source_dir()),
        worker_home=str(worker_home),
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(candidate_repo) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert out.returncode == 99, f"expected crash exit 99, got {out.returncode}: {out.stderr}"

    # Re-open the SAME isolated DB and assert zero authoritative terminal state.
    with kb.isolated_kanban_env(worker_home):
        conn = kb.connect(db_path=worker_home / "kanban.db")
        t = conn.execute(
            "SELECT id FROM tasks WHERE title = 'crash-window-task'"
        ).fetchone()
        child = conn.execute(
            "SELECT id FROM tasks WHERE title = 'crash-child'"
        ).fetchone()
        assert t is not None and child is not None
        tid, cid = t["id"], child["id"]

        row = conn.execute(
            "SELECT status, factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["factory_terminal_receipt_sha256"] is None
        assert kb.list_attachments(conn, tid) == []
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "attached" not in kinds and "completed" not in kinds
        att_dir = kb.task_attachments_dir(tid)
        assert [p for p in att_dir.rglob("*.staging.*")] == []
        # The only residue is an unreachable orphan final file with NO DB row.
        orphan_files = [p for p in att_dir.rglob("aion_monarch_receipt*.json") if p.is_file()]
        assert len(orphan_files) == 1
        assert orphan_files[0].exists()

        # Deterministic recovery: retry completes and wakes the child.
        run_id = int(
            conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        )
        assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, tid).status == "done"
        assert _bound_receipt_doc(conn, tid)["verdict"] == "OUTCOME_ACCEPTED"
        assert kb.get_task(conn, cid).status == "ready"
        # Exactly one bound receipt attachment row, file present, no staging.
        assert len(kb.list_attachments(conn, tid)) == 1
        assert Path(kb.list_attachments(conn, tid)[0].stored_path).exists()
        assert [p for p in att_dir.rglob("*.staging.*")] == []
        conn.close()


# ---------------------------------------------------------------------------
# F5 — ambiguous COMMIT durability reconciliation (R4, R3-F5 repair)
# ---------------------------------------------------------------------------
# The R3 audit reproduced a P0 defect at the COMMIT boundary: when the real
# SQLite COMMIT durably lands and only then the boundary raises, the old
# exception path issued ROLLBACK (a no-op after a landed commit) and then
# discarded the promoted receipt files — deleting the trusted receipt that the
# now-durable done/sha/attachment/event rows reference, losing the dependent
# wake, and leaving ordinary retry on a terminal CAS dead-end. These tests
# prove the R4 reconciliation: the ambiguous COMMIT outcome is resolved
# against the connection's own transaction state (``conn.in_transaction``), so
# a landed commit preserves the receipt and reconciles the dependent wake,
# while a not-landed commit leaves zero residue and retries cleanly.


def test_f5_landed_commit_then_error_preserves_receipt_and_reconciles(
    kanban_home, aion_gov_src, monkeypatch,
):
    """A COMMIT that durably lands and then raises must NOT discard the receipt.

    The ambiguous outcome is reconciled via ``conn.in_transaction``: the commit
    landed (transaction closed), so the promoted receipt is preserved, the
    terminal rows stay consistent, the dependent child wakes, and a retry is
    idempotent (no terminal CAS dead-end)."""
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="factory parent", factory_build_gate=1, assignee="agent007",
        )
        child = kb.create_task(
            conn, title="child", assignee="agent007", parents=[parent],
        )
        run_id = _claim_and_run_id(conn, parent)

        real = kb._execute_boundary_with_retry
        injected = {"done": False}

        def commit_then_raise(c, sql):
            result = real(c, sql)
            if (
                sql.strip().upper() == "COMMIT"
                and kb._AION_PROMOTED_RECEIPT_FILES.get(id(c))
                and not injected["done"]
            ):
                injected["done"] = True
                raise RuntimeError("INJECTED_AMBIGUOUS_COMMIT_AFTER_REAL_COMMIT")
            return result

        monkeypatch.setattr(kb, "_execute_boundary_with_retry", commit_then_raise)

        # The ambiguous COMMIT is reconciled: no exception escapes, the durable
        # terminal state is preserved, and the dependent wake still runs.
        assert kb.complete_task(conn, parent, result="done", expected_run_id=run_id)

        assert kb.get_task(conn, parent).status == "done"
        row = conn.execute(
            "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (parent,),
        ).fetchone()
        assert row["factory_terminal_receipt_sha256"]
        atts = kb.list_attachments(conn, parent)
        assert len(atts) == 1
        assert Path(atts[0].stored_path).exists()  # trusted receipt preserved
        assert "completed" in [e.kind for e in kb.list_events(conn, parent)]
        assert kb.get_task(conn, child).status == "ready"  # dependent wake

        monkeypatch.setattr(kb, "_execute_boundary_with_retry", real)

        # Retry is idempotent: already-done with a valid receipt -> no exception
        # and no terminal CAS dead-end; the receipt remains present.
        assert kb.complete_task(conn, parent, result="retry", expected_run_id=run_id) is False
        assert Path(kb.list_attachments(conn, parent)[0].stored_path).exists()


def test_f5_no_land_commit_error_zero_residue_then_retries(
    kanban_home, aion_gov_src, monkeypatch,
):
    """A COMMIT that fails WITHOUT landing must leave zero mutation/residue.

    ``conn.in_transaction`` is still true, so ROLLBACK truly undoes the writes
    and the staged+promoted receipt files are discarded; a clean retry then
    regenerates them and wakes the dependent."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="factory task", factory_build_gate=1, assignee="agent007",
        )
        child = kb.create_task(conn, title="child", assignee="agent007", parents=[t])
        run_id = _claim_and_run_id(conn, t)

        real = kb._execute_boundary_with_retry
        injected = {"done": False}

        def commit_fails_without_landing(c, sql):
            if sql.strip().upper() == "COMMIT" and not injected["done"]:
                injected["done"] = True
                # Model a commit that fails BEFORE landing: a non-busy
                # OperationalError (no retry) raised without executing the
                # commit, so the transaction stays open (in_transaction True).
                raise sqlite3.OperationalError(
                    "disk I/O error during commit (not landed)"
                )
            return real(c, sql)

        monkeypatch.setattr(kb, "_execute_boundary_with_retry", commit_fails_without_landing)

        with pytest.raises(sqlite3.OperationalError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        _assert_zero_mutation_no_residue(conn, t, run_id)
        assert kb.get_task(conn, child).status == "todo"

        monkeypatch.setattr(kb, "_execute_boundary_with_retry", real)

        assert kb.complete_task(conn, t, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, t).status == "done"
        assert _bound_receipt_doc(conn, t)["verdict"] == "OUTCOME_ACCEPTED"
        assert kb.get_task(conn, child).status == "ready"
        att_dir = kb.task_attachments_dir(t)
        assert len([p for p in att_dir.rglob("aion_monarch_receipt*.json") if p.is_file()]) == 1
        assert [p for p in att_dir.rglob("*.staging.*")] == []


def test_f5_post_commit_invariant_error_preserves_receipt_and_retries(
    kanban_home, aion_gov_src, monkeypatch,
):
    """The post-COMMIT file-length invariant raising must NOT discard the receipt.

    This is the contrast-safe boundary: the rows are already committed and the
    promoted receipt must remain present, so a retry is idempotent rather than
    a terminal CAS dead-end."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="factory task", factory_build_gate=1, assignee="agent007",
        )
        run_id = _claim_and_run_id(conn, t)

        real_invariant = kb._check_file_length_invariant
        injected = {"done": False}

        def invariant_raises_once(conn_arg):
            if not injected["done"]:
                injected["done"] = True
                raise sqlite3.DatabaseError("INJECTED_POST_COMMIT_TORN_EXTEND")
            return real_invariant(conn_arg)

        monkeypatch.setattr(kb, "_check_file_length_invariant", invariant_raises_once)

        with pytest.raises(sqlite3.DatabaseError):
            kb.complete_task(conn, t, result="done", expected_run_id=run_id)

        # Rows committed, receipt preserved (no discard on the invariant path).
        row = conn.execute(
            "SELECT status, factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (t,),
        ).fetchone()
        assert row["status"] == "done"
        assert row["factory_terminal_receipt_sha256"]
        atts = kb.list_attachments(conn, t)
        assert len(atts) == 1
        assert Path(atts[0].stored_path).exists()
        assert "completed" in [e.kind for e in kb.list_events(conn, t)]

        monkeypatch.setattr(kb, "_check_file_length_invariant", real_invariant)

        # Retry is idempotent: no exception, receipt remains present.
        assert kb.complete_task(conn, t, result="retry", expected_run_id=run_id) is False
        assert Path(kb.list_attachments(conn, t)[0].stored_path).exists()


_F5_CRASH_AFTER_COMMIT_SCRIPT = r"""
import os, sys
from pathlib import Path
sys.meta_path[:] = [
    f for f in sys.meta_path
    if "_Editable" not in repr(f) and "Editable" not in f.__class__.__name__
]
sys.path.insert(0, {candidate_repo!r})
os.environ["HERMES_HOME"] = {hermes_home!r}
os.environ["AION_GOVERNANCE_SOURCE_DIR"] = {aion_src!r}
from hermes_cli import kanban_db as kb
with kb.isolated_kanban_env(Path({worker_home!r})):
    kb.init_db()
    conn = kb.connect()
    t = kb.create_task(conn, title="crash-after-commit-task", assignee="agent007", factory_build_gate=1)
    child = kb.create_task(conn, title="crash-after-commit-child", assignee="agent007", parents=[t])
    kb.claim_task(conn, t)
    run_id = int(conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (t,)).fetchone()[0])
    # Crash AFTER the completion transaction commits (durable done/receipt) but
    # BEFORE the dependent wake (recompute_ready) runs.
    def crash_after_commit(*args, **kwargs):
        os._exit(99)
    kb.recompute_ready = crash_after_commit
    kb.complete_task(conn, t, result="done", expected_run_id=run_id)
    os._exit(0)
"""


def test_f5_hard_crash_after_commit_restart_reconciles_dependent_wake(
    kanban_home, aion_gov_src, tmp_path,
):
    """A hard crash after the COMMIT lands (before the dependent wake) must leave
    a consistent terminal state — done + receipt present — and a restart must
    reconcile the dependent wake via recompute_ready, with an idempotent retry."""
    candidate_file = Path(kb.__file__).resolve()
    candidate_repo = candidate_file.parents[1]
    worker_home = tmp_path / "crash_after_commit_home"
    script = _F5_CRASH_AFTER_COMMIT_SCRIPT.format(
        candidate_repo=str(candidate_repo),
        hermes_home=str(Path(os.environ["HERMES_HOME"])),
        aion_src=str(_aion_gov_source_dir()),
        worker_home=str(worker_home),
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(candidate_repo) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert out.returncode == 99, f"expected crash exit 99, got {out.returncode}: {out.stderr}"

    with kb.isolated_kanban_env(worker_home):
        conn = kb.connect(db_path=worker_home / "kanban.db")
        t = conn.execute(
            "SELECT id FROM tasks WHERE title = 'crash-after-commit-task'"
        ).fetchone()
        child = conn.execute(
            "SELECT id FROM tasks WHERE title = 'crash-after-commit-child'"
        ).fetchone()
        assert t is not None and child is not None
        tid, cid = t["id"], child["id"]

        # The completion durably landed before the crash: consistent terminal
        # state with the trusted receipt present.
        row = conn.execute(
            "SELECT status, factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["status"] == "done"
        assert row["factory_terminal_receipt_sha256"]
        atts = kb.list_attachments(conn, tid)
        assert len(atts) == 1
        assert Path(atts[0].stored_path).exists()
        assert "completed" in [e.kind for e in kb.list_events(conn, tid)]

        # The dependent wake was lost by the crash (recompute_ready never ran) ...
        assert kb.get_task(conn, cid).status == "todo"
        # ... but is reconciled by the dispatcher's recompute_ready on restart.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, cid).status == "ready"

        # A restart retry of the completion is idempotent: no exception and no
        # terminal CAS dead-end (the original defect raised
        # FactoryTerminalReceiptRequiredError here). The receipt stays present.
        assert kb.complete_task(conn, tid, result="retry") is False
        assert Path(kb.list_attachments(conn, tid)[0].stored_path).exists()
        conn.close()


# ---------------------------------------------------------------------------
# F5-R6 — opaque (missing in_transaction) landed/non-landed COMMIT boundary
# ---------------------------------------------------------------------------
# The R5 audit (GemAION 4987252999) reproduced ``OPAQUE_LANDED_COMMIT_DELETES_
# DURABLE_RECEIPT_AND_LOSES_IMMEDIATE_WAKE``: a connection proxy that HIDES
# ``in_transaction`` but durably lands the COMMIT was collapsed to NOT-landed,
# so the promoted receipt was deleted, the immediate dependent wake was lost,
# and retry hit a terminal CAS miss. These tests prove the R6 tri-state
# reconciliation (``_ambiguous_commit_landed``) handles BOTH opaque outcomes
# authoritatively: landed preserves the receipt and reconciles terminal success;
# non-landed leaves zero residue and re-raises the original OperationalError.


class _OpaqueLandedProxy:
    """Delegates to a real sqlite3.Connection but HIDES ``in_transaction`` and
    durably lands the COMMIT before raising an OperationalError."""

    def __init__(self, real):
        self._real = real
        self.armed = False
        self.landed_then_raised = False

    def __getattr__(self, name):
        if name == "in_transaction":
            raise AttributeError(name)
        return getattr(self._real, name)

    def execute(self, sql, *args):
        normalized = " ".join(sql.strip().upper().split())
        if normalized == "COMMIT" and self.armed and not self.landed_then_raised:
            self._real.execute(sql, *args)  # durably land the transaction
            self.landed_then_raised = True
            raise sqlite3.OperationalError(
                "injected opaque boundary: COMMIT landed before error"
            )
        return self._real.execute(sql, *args)


class _OpaqueNonLandedProxy:
    """Delegates to a real sqlite3.Connection but HIDES ``in_transaction`` and
    raises on COMMIT WITHOUT landing (transaction stays open)."""

    def __init__(self, real):
        self._real = real
        self.armed = False

    def __getattr__(self, name):
        if name == "in_transaction":
            raise AttributeError(name)
        return getattr(self._real, name)

    def execute(self, sql, *args):
        normalized = " ".join(sql.strip().upper().split())
        if normalized == "COMMIT" and self.armed:
            raise sqlite3.OperationalError(
                "disk I/O error during commit (not landed)"
            )
        return self._real.execute(sql, *args)


def test_f5_opaque_landed_commit_preserves_receipt_and_wakes_dependent(
    kanban_home, aion_gov_src,
):
    """An opaque proxy hiding ``in_transaction`` that LANDS the COMMIT then
    raises must be reconciled as landed: complete returns True, the promoted
    receipt survives, and the child wakes immediately (no restart-only wake)."""
    with kb.connect() as real:
        conn = _OpaqueLandedProxy(real)
        parent = kb.create_task(
            conn, title="opaque-landed-parent", factory_build_gate=1,
            assignee="agent007",
        )
        child = kb.create_task(
            conn, title="opaque-landed-child", assignee="agent007",
            parents=[parent],
        )
        run_id = _claim_and_run_id(conn, parent)
        conn.armed = True

        # Reconcile terminal success: no exception escapes, the durable state is
        # preserved, and the dependent wake still runs (no restart).
        assert kb.complete_task(conn, parent, result="done", expected_run_id=run_id)

        row = conn.execute(
            "SELECT status, factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (parent,),
        ).fetchone()
        assert row["status"] == "done"
        assert row["factory_terminal_receipt_sha256"]
        atts = kb.list_attachments(conn, parent)
        assert len(atts) == 1
        assert Path(atts[0].stored_path).exists()  # trusted receipt preserved
        assert "completed" in [e.kind for e in kb.list_events(conn, parent)]
        assert kb.get_task(conn, child).status == "ready"  # immediate wake

        # Idempotent retry/readback: no exception, no terminal CAS dead-end,
        # receipt remains present.
        assert kb.complete_task(conn, parent, result="retry", expected_run_id=run_id) is False
        assert Path(kb.list_attachments(conn, parent)[0].stored_path).exists()


def test_f5_opaque_nonlanded_commit_reraises_zero_residue_then_retries(
    kanban_home, aion_gov_src,
):
    """An opaque proxy hiding ``in_transaction`` whose COMMIT did NOT land must
    re-raise the original OperationalError, leave zero residue, and permit a
    clean idempotent retry."""
    with kb.connect() as real:
        conn = _OpaqueNonLandedProxy(real)
        parent = kb.create_task(
            conn, title="opaque-noland-parent", factory_build_gate=1,
            assignee="agent007",
        )
        child = kb.create_task(
            conn, title="opaque-noland-child", assignee="agent007",
            parents=[parent],
        )
        run_id = _claim_and_run_id(conn, parent)
        conn.armed = True

        with pytest.raises(sqlite3.OperationalError, match="not landed"):
            kb.complete_task(conn, parent, result="done", expected_run_id=run_id)

        # Zero mutation / zero residue.
        assert kb.get_task(conn, parent).status == "running"
        row = conn.execute(
            "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (parent,),
        ).fetchone()
        assert row["factory_terminal_receipt_sha256"] is None
        assert kb.list_attachments(conn, parent) == []
        kinds = [e.kind for e in kb.list_events(conn, parent)]
        assert "completed" not in kinds
        assert kb.get_task(conn, child).status == "todo"

        # Clean retry succeeds and wakes the dependent.
        conn.armed = False
        assert kb.complete_task(conn, parent, result="done", expected_run_id=run_id)
        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, child).status == "ready"
        assert _bound_receipt_doc(conn, parent)["verdict"] == "OUTCOME_ACCEPTED"
