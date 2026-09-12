"""AUDIT_FIRST_PASS_BLOCKING_PACKET_V1 — bounded-exhaustive first-pass gate.

Regression + hostile coverage for the hard Native invariant on the existing
``kanban_review_verdict`` / durable review-verdict transition: a first-round
``REQUEST_CHANGES`` on an applicable factory exact-head audit must carry a
valid exact-head-bound ``blocking_packet`` with complete required-family
coverage and all known blockers in one packet.  Re-audits (VERIFY) must
disposition prior blockers and classify new ones; from round 3 on, any
non-``PATCH_INTRODUCED`` finding freezes into the ``CONTRACT_TOO_BROAD``
stop-loss disposition instead of another ordinary repair loop.

Applicability is deliberately narrow: the gate only fires when the author task
is ``factory_build_gate=1`` *and* its review handoff carries a structured
exact-head candidate envelope.  Ordinary prose handoffs, non-factory reviews,
and the legitimate PASS path are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + fully rebound Native Kanban env pins."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.isolated_kanban_env(tmp_path):
        kb.init_db()
        yield home


def _candidate(pr=102, head="a" * 40, tree="b" * 40, base="c" * 40):
    return {
        "repository": "kiddhu/hermes-agent",
        "pr": pr,
        "head": head,
        "tree": tree,
        "base": base,
    }


def _structured_reason(candidate):
    return json.dumps(
        {"version": 1, "candidate": candidate, "summary": "exact-head candidate"}
    )


def _family_coverage(overrides=None):
    cov = {
        family: {"disposition": "PASS", "evidence_ref": f"ev-{family}"}
        for family in kb.AUDIT_BLOCKING_REQUIRED_FAMILIES
    }
    if overrides:
        cov.update(overrides)
    return cov


def _blocker(blocker_id, family="contract_scope", **overrides):
    blocker = {
        "id": blocker_id,
        "family": family,
        "invariant": "invariant must hold",
        "severity": "high",
        "evidence_ref": f"ev-{blocker_id}",
        "required_fix": "repair the root cause",
    }
    blocker.update(overrides)
    return blocker


def _packet(candidate, audit_round, blockers, *, disposition="REQUEST_CHANGES",
            mode=None, coverage=None, coverage_complete=True):
    mode = mode or (
        "BOUNDED_EXHAUSTIVE_DISCOVERY" if audit_round == 1 else "VERIFY"
    )
    return {
        "version": 1,
        "audit_round": audit_round,
        "mode": mode,
        "candidate": candidate,
        "family_coverage": coverage if coverage is not None else _family_coverage(),
        "blockers": blockers,
        "coverage_complete": coverage_complete,
        "disposition": disposition,
    }


def _setup_audit(conn, *, structured=True, factory=True, candidate=None):
    """Author (claimed, handed off) + direct auditor child (claimed)."""
    author = kb.create_task(
        conn, title="implementation", assignee="agent007",
        factory_build_gate=1 if factory else 0,
    )
    auditor = kb.create_task(
        conn, title="exact-head audit", assignee="bafuxunan", parents=[author],
        factory_build_gate=1 if factory else 0,
    )
    author_run = kb.claim_task(conn, author)
    assert author_run is not None
    candidate = candidate or _candidate()
    reason = _structured_reason(candidate) if structured else "prose handoff reason"
    receipt = kb.request_review_handoff(
        conn, author, expected_run_id=author_run.current_run_id,
        review_task_id=auditor, reason=reason,
    )
    assert receipt is not None
    auditor_run = kb.claim_task(conn, auditor)
    assert auditor_run is not None
    return author, auditor, candidate, auditor_run.current_run_id


def _round1(conn, author, auditor, auditor_run, candidate, *, packet):
    """Record the round-1 verdict against the setup's already-claimed auditor."""
    return kb.record_review_verdict(
        conn, author, review_task_id=auditor,
        expected_review_run_id=auditor_run,
        verdict="request_changes", reason="request changes",
        blocking_packet=packet,
    )


def _prepare_round(conn, author, auditor, candidate):
    """Claim author, hand off, claim auditor — return the auditor run id."""
    author_run = kb.claim_task(conn, author)
    assert author_run is not None
    receipt = kb.request_review_handoff(
        conn, author, expected_run_id=author_run.current_run_id,
        review_task_id=auditor, reason=_structured_reason(candidate),
    )
    assert receipt is not None
    auditor_run = kb.claim_task(conn, auditor)
    assert auditor_run is not None
    return auditor_run.current_run_id


def _do_round(conn, author, auditor, candidate, *, packet=None,
              verdict="request_changes", reason="request changes"):
    """Prepare a fresh round, then record a verdict."""
    auditor_run = _prepare_round(conn, author, auditor, candidate)
    return kb.record_review_verdict(
        conn, author, review_task_id=auditor,
        expected_review_run_id=auditor_run,
        verdict=verdict, reason=reason, blocking_packet=packet,
    )


def _snapshot(conn, author, auditor):
    author_row = tuple(conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?", (author,),
    ).fetchone())
    child_row = tuple(conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?", (auditor,),
    ).fetchone())
    runs = tuple(tuple(r) for r in conn.execute(
        "SELECT id, task_id, status, outcome, summary FROM task_runs "
        "WHERE task_id IN (?, ?) ORDER BY id", (author, auditor),
    ).fetchall())
    events = tuple(tuple(e) for e in conn.execute(
        "SELECT task_id, kind, payload FROM task_events "
        "WHERE task_id IN (?, ?) ORDER BY id", (author, auditor),
    ).fetchall())
    return author_row, child_row, runs, events


def _verdict_payloads(conn, task_id):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_verdict' ORDER BY id",
        (task_id,),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# ---------------------------------------------------------------------------
# Round-1 negative gates (fail closed, zero state mutation)
# ---------------------------------------------------------------------------

def test_round1_request_changes_without_packet_rejected_zero_mutation(kanban_home):
    with kb.connect() as conn:
        author, auditor, _cand, auditor_run = _setup_audit(conn)
        before = _snapshot(conn, author, auditor)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="missing blocking packet",
        )
        assert ok is False
        assert _snapshot(conn, author, auditor) == before


def test_round1_incomplete_coverage_rejected_zero_mutation(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, auditor_run = _setup_audit(conn)
        # Drop one required family entirely.
        coverage = _family_coverage()
        del coverage["state_lifecycle"]
        packet = _packet(cand, 1, [_blocker("B1")], coverage=coverage)
        before = _snapshot(conn, author, auditor)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="incomplete coverage",
            blocking_packet=packet,
        )
        assert ok is False
        assert _snapshot(conn, author, auditor) == before


def test_round1_stale_round_rejected_zero_mutation(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, auditor_run = _setup_audit(conn)
        packet = _packet(cand, 2, [_blocker("B1")])  # claims round 2, actual round 1
        before = _snapshot(conn, author, auditor)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="stale round",
            blocking_packet=packet,
        )
        assert ok is False
        assert _snapshot(conn, author, auditor) == before


def test_round1_exact_head_mismatch_rejected_zero_mutation(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, auditor_run = _setup_audit(conn)
        wrong = dict(cand, head="f" * 40)  # different head than the handoff
        packet = _packet(wrong, 1, [_blocker("B1")])
        before = _snapshot(conn, author, auditor)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="head mismatch",
            blocking_packet=packet,
        )
        assert ok is False
        assert _snapshot(conn, author, auditor) == before


def test_round1_stop_loss_disposition_rejected(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, auditor_run = _setup_audit(conn)
        packet = _packet(cand, 1, [_blocker("B1")], disposition="CONTRACT_TOO_BROAD")
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="premature stop-loss",
            blocking_packet=packet,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Round-1 positive gate
# ---------------------------------------------------------------------------

def test_round1_multiple_blockers_accepted_and_same_author_resumes_once(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, auditor_run = _setup_audit(conn)
        packet = _packet(
            cand, 1,
            [_blocker("B1", family="contract_scope"),
             _blocker("B2", family="fail_closed")],
        )
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="two blockers, one packet",
            blocking_packet=packet,
        )
        assert ok is True
        author_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (author,),
        ).fetchone()
        assert author_row["status"] == "ready"
        payloads = _verdict_payloads(conn, author)
        assert len(payloads) == 1
        assert payloads[0]["blocking_packet"]["blockers"][0]["id"] == "B1"
        assert payloads[0]["blocking_packet"]["blockers"][1]["id"] == "B2"


# ---------------------------------------------------------------------------
# Re-audit (VERIFY) gates
# ---------------------------------------------------------------------------

def test_reaudit_rejects_missing_prior_blocker_disposition(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, _run = _setup_audit(conn)
        assert _round1(
            conn, author, auditor, _run, cand,
            packet=_packet(cand, 1, [_blocker("A", family="contract_scope")]),
        )
        # Round 2 omits prior blocker A (no disposition) and adds a new blocker.
        auditor_run = _prepare_round(conn, author, auditor, cand)
        before = _snapshot(conn, author, auditor)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="missing prior disposition",
            blocking_packet=_packet(
                cand, 2,
                [_blocker("B", family="fail_closed", classification="PATCH_INTRODUCED")],
            ),
        )
        assert ok is False
        assert _snapshot(conn, author, auditor) == before


def test_reaudit_rejects_new_finding_without_classification(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, _run = _setup_audit(conn)
        assert _round1(
            conn, author, auditor, _run, cand,
            packet=_packet(cand, 1, [_blocker("A", family="contract_scope")]),
        )
        # Round 2 dispositions A but adds new blocker B without a classification.
        auditor_run = _prepare_round(conn, author, auditor, cand)
        before = _snapshot(conn, author, auditor)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="missing classification",
            blocking_packet=_packet(
                cand, 2,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed")],
            ),
        )
        assert ok is False
        assert _snapshot(conn, author, auditor) == before


def test_reaudit_valid_verify_round_accepted(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, _run = _setup_audit(conn)
        assert _round1(
            conn, author, auditor, _run, cand,
            packet=_packet(cand, 1, [_blocker("A", family="contract_scope")]),
        )
        ok = _do_round(
            conn, author, auditor, cand,
            packet=_packet(
                cand, 2,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed", classification="PATCH_INTRODUCED")],
            ),
        )
        assert ok is True
        author_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (author,),
        ).fetchone()
        assert author_row["status"] == "ready"
        payloads = _verdict_payloads(conn, author)
        assert len(payloads) == 2
        assert payloads[1]["blocking_packet"]["audit_round"] == 2


# ---------------------------------------------------------------------------
# Round >= 3 stop-loss
# ---------------------------------------------------------------------------

def test_round3_same_family_non_patch_introduced_yields_stop_loss(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, _run = _setup_audit(conn)
        assert _round1(
            conn, author, auditor, _run, cand,
            packet=_packet(cand, 1, [_blocker("A", family="contract_scope")]),
        )
        assert _do_round(
            conn, author, auditor, cand,
            packet=_packet(
                cand, 2,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed", classification="PATCH_INTRODUCED")],
            ),
        )
        ok = _do_round(
            conn, author, auditor, cand,
            packet=_packet(
                cand, 3,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed", disposition="CLOSED"),
                 _blocker("C", family="contract_scope", classification="AUDIT_MISS")],
                disposition="CONTRACT_TOO_BROAD",
            ),
        )
        assert ok is True
        # Frozen stop-loss: the author is blocked, not resumed into a repair loop.
        author_row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (author,),
        ).fetchone()
        assert author_row["status"] == "blocked"
        assert author_row["block_kind"] == "needs_input"
        payloads = _verdict_payloads(conn, author)
        assert payloads[2]["blocking_packet"]["disposition"] == "CONTRACT_TOO_BROAD"


def test_round3_same_family_requires_stop_loss_disposition(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, _run = _setup_audit(conn)
        assert _round1(
            conn, author, auditor, _run, cand,
            packet=_packet(cand, 1, [_blocker("A", family="contract_scope")]),
        )
        assert _do_round(
            conn, author, auditor, cand,
            packet=_packet(
                cand, 2,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed", classification="PATCH_INTRODUCED")],
            ),
        )
        auditor_run = _prepare_round(conn, author, auditor, cand)
        before = _snapshot(conn, author, auditor)
        # Same non-PATCH_INTRODUCED finding but the auditor tries an ordinary
        # REQUEST_CHANGES disposition — the hard gate must reject it.
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="ordinary repair continuation",
            blocking_packet=_packet(
                cand, 3,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed", disposition="CLOSED"),
                 _blocker("C", family="contract_scope", classification="AUDIT_MISS")],
                disposition="REQUEST_CHANGES",
            ),
        )
        assert ok is False
        assert _snapshot(conn, author, auditor) == before


def test_round3_patch_introduced_still_allows_request_changes(kanban_home):
    with kb.connect() as conn:
        author, auditor, cand, _run = _setup_audit(conn)
        assert _round1(
            conn, author, auditor, _run, cand,
            packet=_packet(cand, 1, [_blocker("A", family="contract_scope")]),
        )
        assert _do_round(
            conn, author, auditor, cand,
            packet=_packet(
                cand, 2,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed", classification="PATCH_INTRODUCED")],
            ),
        )
        # All round-3 blockers are PATCH_INTRODUCED or CLOSED: no stop-loss.
        ok = _do_round(
            conn, author, auditor, cand,
            packet=_packet(
                cand, 3,
                [_blocker("A", family="contract_scope", disposition="CLOSED"),
                 _blocker("B", family="fail_closed", disposition="CLOSED"),
                 _blocker("C", family="state_lifecycle", classification="PATCH_INTRODUCED")],
                disposition="REQUEST_CHANGES",
            ),
        )
        assert ok is True
        author_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (author,),
        ).fetchone()
        assert author_row["status"] == "ready"


# ---------------------------------------------------------------------------
# Applicability boundary: the gate must NOT break legacy / non-factory reviews
# ---------------------------------------------------------------------------

def test_factory_prose_handoff_request_changes_unchanged(kanban_home):
    with kb.connect() as conn:
        author, auditor, _cand, auditor_run = _setup_audit(conn, structured=False)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="legacy prose handoff",
        )
        assert ok is True
        author_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (author,),
        ).fetchone()
        assert author_row["status"] == "ready"


def test_non_factory_structured_handoff_request_changes_unchanged(kanban_home):
    with kb.connect() as conn:
        author, auditor, _cand, auditor_run = _setup_audit(conn, factory=False)
        ok = kb.record_review_verdict(
            conn, author, review_task_id=auditor,
            expected_review_run_id=auditor_run,
            verdict="request_changes", reason="non-factory review",
        )
        assert ok is True


def test_canonical_review_verdict_payload_accepts_plain_pass(kanban_home):
    """A legacy PASS verdict (no blocking_packet) still round-trips."""
    with kb.connect() as conn:
        author, auditor, cand, auditor_run = _setup_audit(conn)
        # Seed a plain PASS-shaped payload and confirm the reader still accepts it.
        payload = {
            "version": 1,
            "review_task_id": auditor,
            "review_run_id": auditor_run,
            "verdict": "pass",
            "reason": "approved",
        }
        with kb.write_txn(conn):
            kb._append_event(conn, author, "review_verdict", payload, run_id=auditor_run)
        rows = conn.execute(
            "SELECT id, run_id, payload FROM task_events WHERE task_id = ? AND kind = 'review_verdict'",
            (author,),
        ).fetchall()
        assert kb._canonical_review_verdict_payload(rows[0]) == payload
