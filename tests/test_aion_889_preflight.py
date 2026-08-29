"""Frozen P1/P2 tests for the AION-889 dual-challenge preflight gate.

This file is intentionally the only test/fixture path in the frozen P1 delta.
The final integration test remains RED until product admission is authorized at
P4; all preceding tests pin the pure checker encoding authorized at P1.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.aion_889_preflight import (
    ACCEPTANCE_CONTRACT_SHA256,
    CHECKER_VERSION,
    GENERIC_RECEIPT_MISSING_BINDINGS,
    REQUIREMENTS_SHA256,
    SOLUTION_CONTRACT_SHA256,
    ChallengeExpectation,
    SemanticAttestationExpectation,
    SemanticPointerBindingError,
    bind_semantic_attestation_in_txn,
    canonical_decision_bytes,
    check_challenge_semantic_bindings,
    check_semantic_attestation,
    compute_scope_identity_sha256,
    strict_json_loads,
    SEMANTIC_MIGRATION_STATE,
    SEMANTIC_RECEIPT_SCHEMA,
    FACTORY_PREFLIGHT_RECEIPT_SCHEMA,
    PREFLIGHT_ACTIVATION_EPOCH,
    PreflightExpectation,
    check_factory_preflight_receipt,
)

SCOPE_IDENTITY_SHA256 = "1d89ce22d02156d562b4fe5ea9e72165068fa6d88edd9ecd17a62e2d1b01171f"

GENERIC_SOLUTION_RECEIPT = {
    "schema": "aion.monarch.trusted_receipt.v1",
    "verdict": "OUTCOME_ACCEPTED",
    "exact_source_refs": {"task_id": "t_2f0f75e5", "run_id": "3315"},
}
GENERIC_ACCEPTANCE_RECEIPT = {
    "schema": "aion.monarch.trusted_receipt.v1",
    "verdict": "OUTCOME_ACCEPTED",
    "exact_source_refs": {"task_id": "t_568a6eb2", "run_id": "3321"},
}


def _expectation(kind: str) -> ChallengeExpectation:
    if kind == "SOLUTION":
        return ChallengeExpectation(
            challenge_kind=kind,
            challenge_verdict="PASS_SOLUTION_CHALLENGE",
            contract_field="solution_contract_sha256",
            contract_sha256=SOLUTION_CONTRACT_SHA256,
            scope_identity_sha256=SCOPE_IDENTITY_SHA256,
            challenge_task_id="t_2f0f75e5",
            challenge_run_id=3315,
            minimum_scope_generation=0,
        )
    return ChallengeExpectation(
        challenge_kind=kind,
        challenge_verdict="PASS_ACCEPTANCE_CHALLENGE",
        contract_field="acceptance_contract_sha256",
        contract_sha256=ACCEPTANCE_CONTRACT_SHA256,
        scope_identity_sha256=SCOPE_IDENTITY_SHA256,
        challenge_task_id="t_568a6eb2",
        challenge_run_id=3321,
        minimum_scope_generation=0,
    )


def _valid_receipt(kind: str) -> dict[str, object]:
    expected = _expectation(kind)
    return {
        "challenge_kind": expected.challenge_kind,
        "challenge_verdict": expected.challenge_verdict,
        expected.contract_field: expected.contract_sha256,
        "scope_identity_sha256": expected.scope_identity_sha256,
        "scope_generation": 0,
        "challenge_task_id": expected.challenge_task_id,
        "challenge_run_id": expected.challenge_run_id,
    }


def test_frozen_hashes_and_checker_version() -> None:
    assert REQUIREMENTS_SHA256 == "bdc47e1acd9dc2e732599c6182c40656c55ef8e5d0b1dad01696be42ad25844c"
    assert SOLUTION_CONTRACT_SHA256 == "7968793f2244ec8942822bc5a9c7319b3c8d65644402d1572d7f8155e290aac6"
    assert ACCEPTANCE_CONTRACT_SHA256 == "9e9e38b94a5f460fa16870f38b0af1ff1873f829c75d786cedd21184989a9630"
    assert CHECKER_VERSION == "AION_DUAL_CHALLENGE_PREFLIGHT_CHECKER_V2"


def test_scope_identity_matches_pre_code_anchor() -> None:
    assert compute_scope_identity_sha256(
        directive_id="AION-889-PREFLIGHT-V1-IMPLEMENT",
        risk_tier="T2_CORE_OR_HIGH_RISK",
        requirements_sha256=REQUIREMENTS_SHA256,
        solution_sha256=SOLUTION_CONTRACT_SHA256,
        acceptance_sha256=ACCEPTANCE_CONTRACT_SHA256,
    ) == SCOPE_IDENTITY_SHA256


@pytest.mark.parametrize(
    ("receipt", "kind"),
    [(GENERIC_SOLUTION_RECEIPT, "SOLUTION"), (GENERIC_ACCEPTANCE_RECEIPT, "ACCEPTANCE")],
)
def test_current_generic_terminal_receipts_are_explicit_negative_fixtures(
    receipt: dict[str, object], kind: str
) -> None:
    result = check_challenge_semantic_bindings(receipt, _expectation(kind))
    assert result.decision == "BLOCK"
    assert result.baseline_reason_code == GENERIC_RECEIPT_MISSING_BINDINGS
    assert result.missing_fields == (
        "challenge_kind",
        "challenge_verdict",
        _expectation(kind).contract_field,
        "scope_identity_sha256",
        "scope_generation",
        "challenge_task_id",
        "challenge_run_id",
    )


@pytest.mark.parametrize("kind", ["SOLUTION", "ACCEPTANCE"])
def test_exact_semantic_bindings_pass(kind: str) -> None:
    result = check_challenge_semantic_bindings(_valid_receipt(kind), _expectation(kind))
    assert result.decision == "PASS"
    assert result.baseline_reason_code is None
    assert result.missing_fields == ()


@pytest.mark.parametrize(
    "field",
    [
        "challenge_kind",
        "challenge_verdict",
        "solution_contract_sha256",
        "scope_identity_sha256",
        "scope_generation",
        "challenge_task_id",
        "challenge_run_id",
    ],
)
def test_each_solution_semantic_binding_is_individually_required(field: str) -> None:
    receipt = _valid_receipt("SOLUTION")
    del receipt[field]
    result = check_challenge_semantic_bindings(receipt, _expectation("SOLUTION"))
    assert result.decision == "BLOCK"
    assert result.baseline_reason_code == GENERIC_RECEIPT_MISSING_BINDINGS
    assert result.detail_code == "MISSING_FIELD"
    assert result.field == field
    assert result.missing_fields == (field,)


@pytest.mark.parametrize(
    ("mutate", "detail_code"),
    [
        (lambda r: r.update(challenge_kind="ACCEPTANCE"), "CHALLENGE_KIND_MISMATCH"),
        (lambda r: r.update(challenge_verdict="OUTCOME_ACCEPTED"), "CHALLENGE_NOT_PASS"),
        (lambda r: r.update(solution_contract_sha256="0" * 64), "HASH_MISMATCH"),
        (lambda r: r.update(scope_identity_sha256="0" * 64), "SCOPE_BINDING_MISMATCH"),
        (lambda r: r.update(scope_generation=-1), "SCOPE_GENERATION_INVALID"),
        (lambda r: r.update(challenge_task_id="t_stale"), "STALE_CURRENT_CHALLENGE"),
        (lambda r: r.update(challenge_run_id=3314), "CHALLENGE_RUN_MISMATCH"),
    ],
)
def test_solution_semantic_binding_mismatches_fail_closed(mutate, detail_code: str) -> None:
    receipt = _valid_receipt("SOLUTION")
    mutate(receipt)
    result = check_challenge_semantic_bindings(receipt, _expectation("SOLUTION"))
    assert result.decision == "BLOCK"
    assert result.detail_code == detail_code


def test_strict_json_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        strict_json_loads('{"challenge_kind":"SOLUTION","challenge_kind":"ACCEPTANCE"}')


def test_canonical_decision_bytes_are_stable_and_closed() -> None:
    payload = canonical_decision_bytes(
        decision="BLOCK",
        reason_code="CHALLENGE_INVALID",
        detail_code="CHALLENGE_NOT_PASS",
        field="solution_challenge_verdict",
        evidence_ref=None,
    )
    assert payload == (
        b'{"decision":"BLOCK","reason_code":"CHALLENGE_INVALID",'
        b'"detail_code":"CHALLENGE_NOT_PASS",'
        b'"field":"solution_challenge_verdict","evidence_ref":null}'
    )
    assert hashlib.sha256(payload).hexdigest() == hashlib.sha256(bytes(payload)).hexdigest()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_in_scope_claim_without_preflight_receipt_blocks_before_run_row(
    kanban_home: Path,
) -> None:
    """P2 registry-owned RED: current product still admits without #889 receipt."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="AION-889 in-scope implementation fixture",
            assignee="agent007",
            factory_build_gate=1,
            factory_directive_id="AION-889-PREFLIGHT-V1-IMPLEMENT",
        )
        claimed = kb.claim_task(conn, task_id, claimer="aion-889-red:1")
        task = kb.get_task(conn, task_id)
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        decisions = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "preflight_decision"
        ]

    if claimed is not None:
        pytest.fail(
            "AION_889_EXPECTED_RED::MISSING_RECEIPT_POINTER::"
            "product_admitted_without_preflight",
            pytrace=False,
        )
    assert task is not None and task.status == "blocked"
    assert run_count == 0
    assert len(decisions) == 1
    assert decisions[0].payload == {
        "decision": "BLOCK",
        "reason_code": "RECEIPT_INVALID",
        "detail_code": "MISSING_RECEIPT_POINTER",
        "field": "factory_preflight_receipt_sha256",
        "evidence_ref": None,
    }


TEST_FIRST_SHA256 = "3" * 64
CHECKER_OUTPUT_SHA256 = "4" * 64


def _preflight_receipt(task_id: str) -> dict[str, object]:
    return {
        "schema": FACTORY_PREFLIGHT_RECEIPT_SCHEMA,
        "task_id": task_id,
        "risk_tier": "T2_CORE_OR_HIGH_RISK",
        "outcome_requirement_sha256": REQUIREMENTS_SHA256,
        "solution_contract_sha256": SOLUTION_CONTRACT_SHA256,
        "solution_challenge_verdict": "PASS_SOLUTION_CHALLENGE",
        "acceptance_contract_sha256": ACCEPTANCE_CONTRACT_SHA256,
        "acceptance_challenge_verdict": "PASS_ACCEPTANCE_CHALLENGE",
        "test_first_evidence": TEST_FIRST_SHA256,
        "preflight_checker_version": CHECKER_VERSION,
        "preflight_verdict": "PASS",
        "scope_identity_sha256": SCOPE_IDENTITY_SHA256,
        "checker_output_sha256": CHECKER_OUTPUT_SHA256,
    }


def _preflight_expectation(task_id: str) -> PreflightExpectation:
    return PreflightExpectation(task_id=task_id)


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("outcome_requirement_sha256", "0" * 64, "HASH_MISMATCH"),
        ("solution_contract_sha256", "0" * 64, "HASH_MISMATCH"),
        ("solution_challenge_verdict", "OUTCOME_ACCEPTED", "CHALLENGE_NOT_PASS"),
        ("acceptance_contract_sha256", "0" * 64, "HASH_MISMATCH"),
        ("acceptance_challenge_verdict", "OUTCOME_ACCEPTED", "CHALLENGE_NOT_PASS"),
        ("test_first_evidence", "", "TEST_FIRST_EVIDENCE_INVALID"),
        ("preflight_checker_version", "stale", "CHECKER_VERSION_MISMATCH"),
        ("preflight_verdict", "BLOCK", "PREFLIGHT_NOT_PASS"),
        ("scope_identity_sha256", "0" * 64, "SCOPE_BINDING_MISMATCH"),
        ("checker_output_sha256", "", "CHECKER_OUTPUT_INVALID"),
    ],
)
def test_each_invalid_preflight_machine_field_fails_closed(field, value, detail) -> None:
    receipt = _preflight_receipt("t_fixture")
    receipt[field] = value
    result = check_factory_preflight_receipt(receipt, _preflight_expectation("t_fixture"))
    assert result.decision == "BLOCK"
    assert result.detail_code == detail
    assert result.field == field


@pytest.mark.parametrize("field", list(_preflight_receipt("t_fixture")))
def test_each_preflight_field_is_required(field: str) -> None:
    receipt = _preflight_receipt("t_fixture")
    del receipt[field]
    result = check_factory_preflight_receipt(receipt, _preflight_expectation("t_fixture"))
    assert result.decision == "BLOCK"
    assert result.detail_code == "MISSING_FIELD"
    assert result.field == field


def _create_in_scope_task(conn) -> str:
    task_id = kb.create_task(
        conn,
        title="AION-889 active preflight fixture",
        assignee="agent007",
        factory_build_gate=1,
        factory_directive_id="AION-889-PREFLIGHT-V1-IMPLEMENT",
    )
    conn.execute(
        "UPDATE tasks SET created_at=? WHERE id=?",
        (PREFLIGHT_ACTIVATION_EPOCH + 1, task_id),
    )
    return task_id


def _bind_valid_preflight(conn, task_id: str) -> str:
    raw = json.dumps(
        _preflight_receipt(task_id), sort_keys=True, separators=(",", ":")
    ).encode()
    attachment_id = kb.store_attachment_bytes(
        conn,
        task_id,
        "aion_factory_preflight_receipt.json",
        raw,
        content_type="application/json",
        uploaded_by="aion_monarch_proof_kernel",
    )
    return kb.bind_factory_preflight_receipt(conn, task_id, attachment_id)


def test_valid_frozen_packet_admits_exactly_once(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = _create_in_scope_task(conn)
        receipt_sha = _bind_valid_preflight(conn, task_id)
        first = kb.claim_task(conn, task_id, claimer="preflight-valid:1")
        second = kb.claim_task(conn, task_id, claimer="preflight-valid:2")
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        pointer = conn.execute(
            "SELECT factory_preflight_receipt_sha256 FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    assert first is not None
    assert second is None
    assert run_count == 1
    assert pointer == receipt_sha


def test_forged_uploader_blocks_before_run_row(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = _create_in_scope_task(conn)
        raw = json.dumps(
            _preflight_receipt(task_id), sort_keys=True, separators=(",", ":")
        ).encode()
        kb.store_attachment_bytes(conn, task_id, "forged.json", raw, uploaded_by="agent")
        sha = hashlib.sha256(raw).hexdigest()
        conn.execute(
            "UPDATE tasks SET factory_preflight_receipt_sha256=? WHERE id=?",
            (sha, task_id),
        )
        assert kb.claim_task(conn, task_id, claimer="preflight-forged:1") is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 0
        event = [
            e for e in kb.list_events(conn, task_id) if e.kind == "preflight_decision"
        ][-1]
    assert event.payload["detail_code"] == "RECEIPT_PROVENANCE_INVALID"


def test_review_claim_surface_also_blocks_before_run_row(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = _create_in_scope_task(conn)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        assert kb.claim_review_task(conn, task_id, claimer="preflight-review:1") is None
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 0


def test_block_bind_unblock_recovery_is_deterministic(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = _create_in_scope_task(conn)
        assert kb.claim_task(conn, task_id, claimer="recovery:blocked") is None
        assert kb.get_task(conn, task_id).status == "blocked"
        _bind_valid_preflight(conn, task_id)
        assert kb.unblock_task(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer="recovery:pass")
        decisions = [
            e.payload for e in kb.list_events(conn, task_id)
            if e.kind == "preflight_decision"
        ]
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    assert claimed is not None
    assert [d["decision"] for d in decisions] == ["BLOCK", "PASS"]
    assert run_count == 1


def test_preflight_pointer_is_immutable(kanban_home: Path) -> None:
    with kb.connect() as conn:
        task_id = _create_in_scope_task(conn)
        _bind_valid_preflight(conn, task_id)
        with pytest.raises(sqlite3.IntegrityError, match="pointer is immutable"):
            conn.execute(
                "UPDATE tasks SET factory_preflight_receipt_sha256=? WHERE id=?",
                ("f" * 64, task_id),
            )


def test_cross_task_preflight_replay_blocks(kanban_home: Path) -> None:
    with kb.connect() as conn:
        source = _create_in_scope_task(conn)
        target = _create_in_scope_task(conn)
        raw = json.dumps(
            _preflight_receipt(source), sort_keys=True, separators=(",", ":")
        ).encode()
        kb.store_attachment_bytes(
            conn,
            target,
            "replay.json",
            raw,
            uploaded_by="aion_monarch_proof_kernel",
        )
        sha = hashlib.sha256(raw).hexdigest()
        conn.execute(
            "UPDATE tasks SET factory_preflight_receipt_sha256=? WHERE id=?",
            (sha, target),
        )
        assert kb.claim_task(conn, target, claimer="replay:1") is None
        event = [
            e for e in kb.list_events(conn, target) if e.kind == "preflight_decision"
        ][-1]
    assert event.payload["detail_code"] == "TASK_BINDING_MISMATCH"
    assert event.payload["field"] == "task_id"


def test_non_directive_and_other_profile_paths_remain_unchanged(kanban_home: Path) -> None:
    with kb.connect() as conn:
        legacy = kb.create_task(
            conn, title="legacy factory", assignee="agent007", factory_build_gate=1
        )
        other = kb.create_task(
            conn,
            title="same directive other profile",
            assignee="gm2",
            factory_build_gate=1,
            factory_directive_id="AION-889-PREFLIGHT-V1-IMPLEMENT",
        )
        assert kb.claim_task(conn, legacy, claimer="legacy:1") is not None
        assert kb.claim_task(conn, other, claimer="other:1") is not None


CANONICAL_NATIVE_DIRECTIVE_BODY = """strategic_directive:
  directive_id: AION-889-PREFLIGHT-V1-IMPLEMENT
  source: AION-GM2 / 13爷
  objective: fail closed before implementation pickup
"""


@pytest.mark.parametrize(("source_status", "claim_name"), [("ready", "claim_task"), ("review", "claim_review_task")])
def test_canonical_nested_native_directive_requires_preflight_before_pickup(
    kanban_home: Path, source_status: str, claim_name: str,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="canonical nested AION-889 directive",
            body=CANONICAL_NATIVE_DIRECTIVE_BODY,
            assignee="agent007",
            factory_build_gate=1,
        )
        if source_status == "review":
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        claimed = getattr(kb, claim_name)(conn, task_id, claimer=f"nested:{source_status}")
        row = conn.execute(
            "SELECT status, factory_preflight_required FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        decisions = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "preflight_decision"
        ]
    assert claimed is None
    assert tuple(row) == ("blocked", 1)
    assert run_count == 0
    assert len(decisions) == 1
    assert decisions[0].payload["detail_code"] == "MISSING_RECEIPT_POINTER"


@pytest.mark.parametrize(("source_status", "claim_name"), [("ready", "claim_task"), ("review", "claim_review_task")])
def test_preupgrade_in_scope_row_is_migrated_fail_closed_at_pickup(
    kanban_home: Path, source_status: str, claim_name: str,
) -> None:
    """Model an exact-base row migrated with the new requirement bit defaulting to 0."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="pre-upgrade AION-889 directive",
            body="legacy row before preflight columns\n",
            assignee="agent007",
            factory_build_gate=1,
        )
        conn.execute(
            "UPDATE tasks SET body=?, status=?, created_at=? WHERE id=?",
            (
                "factory_directive_id: AION-889-PREFLIGHT-V1-IMPLEMENT\n",
                source_status,
                PREFLIGHT_ACTIVATION_EPOCH + 1,
                task_id,
            ),
        )
        claimed = getattr(kb, claim_name)(conn, task_id, claimer=f"upgrade:{source_status}")
        row = conn.execute(
            "SELECT status, factory_preflight_required FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    assert claimed is None
    assert tuple(row) == ("blocked", 1)
    assert run_count == 0


@pytest.mark.parametrize(("source_status", "claim_name"), [("ready", "claim_task"), ("review", "claim_review_task")])
def test_pre_activation_directive_resumed_after_activation_is_fail_closed(
    kanban_home: Path, source_status: str, claim_name: str,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="resumed AION-889 directive",
            body="legacy row before preflight columns\n",
            assignee="agent007",
            factory_build_gate=1,
        )
        conn.execute(
            "UPDATE tasks SET body=?, status=?, created_at=? WHERE id=?",
            (
                CANONICAL_NATIVE_DIRECTIVE_BODY,
                source_status,
                PREFLIGHT_ACTIVATION_EPOCH - 1,
                task_id,
            ),
        )
        kb._append_event(conn, task_id, "unblocked", {"reason": "resume exact task"})
        claimed = getattr(kb, claim_name)(conn, task_id, claimer=f"resume:{source_status}")
        row = conn.execute(
            "SELECT status, factory_preflight_required FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        run_count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    assert claimed is None
    assert tuple(row) == ("blocked", 1)
    assert run_count == 0


def test_pre_activation_directive_without_resume_remains_legacy(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="historical AION-889 directive",
            body="legacy row before preflight columns\n",
            assignee="agent007",
            factory_build_gate=1,
        )
        conn.execute(
            "UPDATE tasks SET body=?, created_at=? WHERE id=?",
            (CANONICAL_NATIVE_DIRECTIVE_BODY, PREFLIGHT_ACTIVATION_EPOCH - 1, task_id),
        )
        claimed = kb.claim_task(conn, task_id, claimer="historical:ready")
        requirement = conn.execute(
            "SELECT factory_preflight_required FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    assert claimed is not None
    assert requirement == 0


# ---------------------------------------------------------------------------
# P2.5 trusted-evidence carrier bootstrap (MIGRATED_DISABLED; no claim wiring)
# ---------------------------------------------------------------------------

GENERIC_SOLUTION_SHA = "d6b082eb4c14fb1003472c38d377d65ae45da12cb26dd8848a47255474bc1553"
GOVERNANCE_BASE_SHA = "5c74e39e8f38907fbb32305ac21adb1e7b61e321"
HERMES_P1_HEAD_SHA = "68110f5723cb3bc3d93c9bd5f9e431fa091ff6a9"


def _semantic_expectation() -> SemanticAttestationExpectation:
    return SemanticAttestationExpectation(
        challenge=_expectation("SOLUTION"),
        source_factory_terminal_receipt_sha256=GENERIC_SOLUTION_SHA,
        governance_base_sha=GOVERNANCE_BASE_SHA,
        hermes_checker_head_sha=HERMES_P1_HEAD_SHA,
    )


def _semantic_receipt() -> dict[str, object]:
    return {
        "schema": SEMANTIC_RECEIPT_SCHEMA,
        "migration_state": SEMANTIC_MIGRATION_STATE,
        **_valid_receipt("SOLUTION"),
        "source_factory_terminal_receipt_sha256": GENERIC_SOLUTION_SHA,
        "attested_by": "aion_monarch_proof_kernel",
        "bootstrap_binding": {
            "governance_base_sha": GOVERNANCE_BASE_SHA,
            "hermes_checker_head_sha": HERMES_P1_HEAD_SHA,
        },
    }


def test_future_scope_generation_is_replay_and_fails_exact_current_binding() -> None:
    receipt = _valid_receipt("SOLUTION")
    receipt["scope_generation"] = 1
    result = check_challenge_semantic_bindings(receipt, _expectation("SOLUTION"))
    assert result.decision == "BLOCK"
    assert result.detail_code == "STALE_CURRENT_CHALLENGE"
    assert result.field == "scope_generation"


def test_migrated_disabled_semantic_attestation_passes_pure_readback_only() -> None:
    result = check_semantic_attestation(_semantic_receipt(), _semantic_expectation())
    assert result.decision == "PASS"


@pytest.mark.parametrize(
    ("mutate", "detail", "field"),
    [
        (lambda r: r.update(attested_by="agent"), "RECEIPT_PROVENANCE_INVALID", "attested_by"),
        (lambda r: r.update(migration_state="ACTIVE"), "MIGRATION_STATE_INVALID", "migration_state"),
        (
            lambda r: r.update(source_factory_terminal_receipt_sha256="f" * 64),
            "SOURCE_RECEIPT_STALE",
            "source_factory_terminal_receipt_sha256",
        ),
        (
            lambda r: r["bootstrap_binding"].update(governance_base_sha="1" * 40),
            "BOOTSTRAP_HEAD_MISMATCH",
            "governance_base_sha",
        ),
        (
            lambda r: r["bootstrap_binding"].update(hermes_checker_head_sha="2" * 40),
            "BOOTSTRAP_HEAD_MISMATCH",
            "hermes_checker_head_sha",
        ),
    ],
)
def test_semantic_attestation_hostile_matrix_fails_closed(mutate, detail, field) -> None:
    receipt = _semantic_receipt()
    mutate(receipt)
    result = check_semantic_attestation(receipt, _semantic_expectation())
    assert result.decision == "BLOCK"
    assert result.detail_code == detail
    assert result.field == field


def test_disabled_pointer_schema_is_additive_and_immutable(kanban_home: Path) -> None:
    with kb.connect() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "factory_challenge_semantic_receipt_sha256" in columns
        task_id = kb.create_task(conn, title="semantic pointer fixture")
        conn.execute(
            "UPDATE tasks SET factory_challenge_semantic_receipt_sha256=? WHERE id=?",
            ("a" * 64, task_id),
        )
        with pytest.raises(Exception, match="immutable"):
            conn.execute(
                "UPDATE tasks SET factory_challenge_semantic_receipt_sha256=? WHERE id=?",
                ("b" * 64, task_id),
            )
        row = conn.execute(
            "SELECT factory_terminal_receipt_sha256, "
            "factory_challenge_semantic_receipt_sha256 FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        assert row["factory_terminal_receipt_sha256"] is None
        assert row["factory_challenge_semantic_receipt_sha256"] == "a" * 64


def test_host_binder_is_cas_idempotent_and_never_relabels_source() -> None:
    state = {
        "source_sha": GENERIC_SOLUTION_SHA,
        "semantic_sha": None,
        "semantic_uploaded_by": None,
        "semantic_bytes_sha": None,
    }
    calls: list[str] = []

    def read_binding(conn, task_id):
        return dict(state)

    def store_attachment(conn, task_id, filename, raw, *, uploaded_by, board):
        calls.append("attachment")
        assert uploaded_by == "aion_monarch_proof_kernel"
        return 17

    def cas(conn, task_id, expected_source, expected_semantic, new_sha):
        calls.append("cas")
        if state["source_sha"] != expected_source or state["semantic_sha"] is not expected_semantic:
            return False
        state["semantic_sha"] = new_sha
        state["semantic_uploaded_by"] = "aion_monarch_proof_kernel"
        state["semantic_bytes_sha"] = new_sha
        return True

    def event(conn, task_id, kind, payload):
        calls.append("event")

    raw = json.dumps(_semantic_receipt(), sort_keys=True, separators=(",", ":")).encode()
    first = bind_semantic_attestation_in_txn(
        conn=object(), board="aion-factory", task_id="t_2f0f75e5", raw=raw,
        expected=_semantic_expectation(), read_binding=read_binding,
        store_attachment=store_attachment, compare_and_swap_pointer=cas,
        append_event=event,
    )
    second = bind_semantic_attestation_in_txn(
        conn=object(), board="aion-factory", task_id="t_2f0f75e5", raw=raw,
        expected=_semantic_expectation(), read_binding=read_binding,
        store_attachment=store_attachment, compare_and_swap_pointer=cas,
        append_event=event,
    )
    assert first["idempotent"] is False and second["idempotent"] is True
    assert calls == ["attachment", "cas", "event"]
    assert state["source_sha"] == GENERIC_SOLUTION_SHA


def test_host_binder_rejects_pointer_collision_without_write() -> None:
    state = {
        "source_sha": GENERIC_SOLUTION_SHA,
        "semantic_sha": "e" * 64,
        "semantic_uploaded_by": "aion_monarch_proof_kernel",
        "semantic_bytes_sha": "e" * 64,
    }
    raw = json.dumps(_semantic_receipt(), sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(SemanticPointerBindingError, match="SEMANTIC_POINTER_COLLISION"):
        bind_semantic_attestation_in_txn(
            conn=object(), board="aion-factory", task_id="t_2f0f75e5", raw=raw,
            expected=_semantic_expectation(), read_binding=lambda *_: dict(state),
            store_attachment=lambda *a, **k: pytest.fail("must not write"),
            compare_and_swap_pointer=lambda *a: pytest.fail("must not CAS"),
            append_event=lambda *a: pytest.fail("must not emit"),
        )
