"""Frozen P1/P2 tests for the AION-889 dual-challenge preflight gate.

This file is intentionally the only test/fixture path in the frozen P1 delta.
The final integration test remains RED until product admission is authorized at
P4; all preceding tests pin the pure checker encoding authorized at P1.
"""

from __future__ import annotations

import hashlib
import json
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
    canonical_decision_bytes,
    check_challenge_semantic_bindings,
    compute_scope_identity_sha256,
    strict_json_loads,
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
