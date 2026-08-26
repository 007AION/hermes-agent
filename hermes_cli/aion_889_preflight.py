"""Pure deterministic checker primitives for AION-889 P1 encoding.

This module is deliberately side-effect free: no DB, filesystem, network,
clock, randomness, locale, or LLM access.  Product admission wiring remains
outside this P1 test/checker-only delta until the P3 machine gate passes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

REQUIREMENTS_SHA256 = "bdc47e1acd9dc2e732599c6182c40656c55ef8e5d0b1dad01696be42ad25844c"
SOLUTION_CONTRACT_SHA256 = "7968793f2244ec8942822bc5a9c7319b3c8d65644402d1572d7f8155e290aac6"
ACCEPTANCE_CONTRACT_SHA256 = "9e9e38b94a5f460fa16870f38b0af1ff1873f829c75d786cedd21184989a9630"
CHECKER_VERSION = "AION_DUAL_CHALLENGE_PREFLIGHT_CHECKER_V2"

# GM2's P1 baseline diagnostic for the two current generic terminal receipts.
# It is evidence-fixture vocabulary, not a new canonical admission detail code;
# the frozen Acceptance V3 detail-code mapping remains unchanged.
GENERIC_RECEIPT_MISSING_BINDINGS = (
    "CHALLENGE_TERMINAL_RECEIPT_MISSING_SEMANTIC_BINDINGS"
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DECISION_KEYS = (
    "decision",
    "reason_code",
    "detail_code",
    "field",
    "evidence_ref",
)


@dataclass(frozen=True)
class ChallengeExpectation:
    challenge_kind: str
    challenge_verdict: str
    contract_field: str
    contract_sha256: str
    scope_identity_sha256: str
    challenge_task_id: str
    challenge_run_id: int
    minimum_scope_generation: int


@dataclass(frozen=True)
class ChallengeBindingResult:
    decision: str
    baseline_reason_code: str | None
    detail_code: str | None
    field: str | None
    missing_fields: tuple[str, ...]


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str | bytes) -> Any:
    """Parse JSON while rejecting duplicate keys at every object level."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)


def compute_scope_identity_sha256(
    *,
    directive_id: str,
    risk_tier: str,
    requirements_sha256: str,
    solution_sha256: str,
    acceptance_sha256: str,
) -> str:
    """Return Acceptance V3's compact sort-key scope identity."""
    payload = {
        "acceptance_sha256": acceptance_sha256,
        "directive_id": directive_id,
        "requirements_sha256": requirements_sha256,
        "risk_tier": risk_tier,
        "solution_sha256": solution_sha256,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _block(detail_code: str, field: str) -> ChallengeBindingResult:
    return ChallengeBindingResult(
        decision="BLOCK",
        baseline_reason_code=None,
        detail_code=detail_code,
        field=field,
        missing_fields=(),
    )


def check_challenge_semantic_bindings(
    receipt: dict[str, Any],
    expected: ChallengeExpectation,
) -> ChallengeBindingResult:
    """Validate the semantic fields absent from legacy terminal receipts.

    The function consumes already-selected persisted receipt bytes plus the
    deterministic current challenge expectation.  Selection, provenance, and
    DB transaction enforcement are later admission-layer responsibilities.
    """
    required = (
        "challenge_kind",
        "challenge_verdict",
        expected.contract_field,
        "scope_identity_sha256",
        "scope_generation",
        "challenge_task_id",
        "challenge_run_id",
    )
    missing = tuple(field for field in required if field not in receipt)
    if missing:
        return ChallengeBindingResult(
            decision="BLOCK",
            baseline_reason_code=GENERIC_RECEIPT_MISSING_BINDINGS,
            detail_code="MISSING_FIELD",
            field=missing[0],
            missing_fields=missing,
        )

    if receipt["challenge_kind"] != expected.challenge_kind:
        return _block("CHALLENGE_KIND_MISMATCH", "challenge_kind")
    if receipt["challenge_verdict"] != expected.challenge_verdict:
        return _block("CHALLENGE_NOT_PASS", "challenge_verdict")

    contract_sha = receipt[expected.contract_field]
    if not isinstance(contract_sha, str) or not _HEX64.fullmatch(contract_sha):
        return _block("MALFORMED_FIELD", expected.contract_field)
    if contract_sha != expected.contract_sha256:
        return _block("HASH_MISMATCH", expected.contract_field)

    scope_sha = receipt["scope_identity_sha256"]
    if not isinstance(scope_sha, str) or not _HEX64.fullmatch(scope_sha):
        return _block("MALFORMED_FIELD", "scope_identity_sha256")
    if scope_sha != expected.scope_identity_sha256:
        return _block("SCOPE_BINDING_MISMATCH", "scope_identity_sha256")

    generation = receipt["scope_generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return _block("SCOPE_GENERATION_INVALID", "scope_generation")
    if generation < expected.minimum_scope_generation:
        return _block("STALE_CURRENT_CHALLENGE", "scope_generation")

    if receipt["challenge_task_id"] != expected.challenge_task_id:
        return _block("STALE_CURRENT_CHALLENGE", "challenge_task_id")
    run_id = receipt["challenge_run_id"]
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        return _block("MALFORMED_FIELD", "challenge_run_id")
    if run_id != expected.challenge_run_id:
        return _block("CHALLENGE_RUN_MISMATCH", "challenge_run_id")

    return ChallengeBindingResult(
        decision="PASS",
        baseline_reason_code=None,
        detail_code=None,
        field=None,
        missing_fields=(),
    )


def canonical_decision_bytes(
    *,
    decision: str,
    reason_code: str | None,
    detail_code: str | None,
    field: str | None,
    evidence_ref: dict[str, Any] | None,
) -> bytes:
    """Encode the closed five-key Acceptance V3 decision object."""
    if decision not in {"PASS", "BLOCK"}:
        raise ValueError("decision must be PASS or BLOCK")
    if evidence_ref is not None:
        if tuple(evidence_ref) != ("attachment_id", "sha256"):
            raise ValueError("evidence_ref keys/order invalid")
        attachment_id = evidence_ref["attachment_id"]
        sha256 = evidence_ref["sha256"]
        if (
            isinstance(attachment_id, bool)
            or not isinstance(attachment_id, int)
            or attachment_id <= 0
            or not isinstance(sha256, str)
            or not _HEX64.fullmatch(sha256)
        ):
            raise ValueError("evidence_ref values invalid")
    payload = {
        "decision": decision,
        "reason_code": reason_code,
        "detail_code": detail_code,
        "field": field,
        "evidence_ref": evidence_ref,
    }
    if tuple(payload) != _DECISION_KEYS:
        raise AssertionError("internal decision key order drift")
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")
