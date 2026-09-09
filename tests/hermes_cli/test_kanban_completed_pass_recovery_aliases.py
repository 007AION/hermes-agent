"""Alias normalization for completed-audit PASS recovery receipts.

The installed completed-audit PASS recovery parser historically accepted only
one field spelling: ``head`` + ``review_outcome == "approved"``.  The two real
terminal recovery schemas emitted by the existing audited paths use aliases:

  * ``t_ec036f47`` run 4276 (PR #94 repeated-audit) — ``head`` present plus
    ``verdict == "PASS_EXACT_HEAD"``, no ``review_outcome``.
  * ``t_d7a8d4dc`` run 4268 (PR #87) — ``exact_head`` present plus an exact
    commit-bound GitHub ``APPROVED`` receipt, no top-level ``head``.

``_normalize_completed_pass_recovery_receipt`` maps both spellings to one
canonical internal receipt only when every semantic identity field is present,
unique, non-conflicting and corroborated by the exact commit-bound GitHub
``APPROVED`` readback.  Any missing/conflicting/ambiguous alias, wrong head/
tree/base/review id/url/auditor, non-APPROVED state, or snapshot drift must
fail closed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# Exact commit identity copied from the live terminal runs (read-only).
HEAD_4276 = "bfc06e96fe9c0fe9e1d745fbac6da0d1bd309aa7"
TREE_4276 = "1c08f1eae0dea83bef2a746309a2374b521b932d"
BASE_4276 = "b2a0b8a410b0ec8545632bd9447cc8a212bd653a"
PR_4276 = 94
REVIEW_ID_4276 = 5152598812

EXACT_HEAD_4268 = "b563d45dcfec776599f5f3be8e366caca4e1783c"
TREE_4268 = "a9ffaff78f49b022adef753b7ef3e64ea2b27ce8"
BASE_4268 = "ef11089a57a1aec9adfe5d2b25e03ca0c50d1c69"
PR_4268 = 87
REVIEW_ID_4268 = 5151368236


def _receipt(head, tree, base, pr, review_id):
    return {
        "review_outcome": "approved",
        "repository": "kiddhu/hermes-agent",
        "pr": pr,
        "head": head,
        "tree": tree,
        "base": base,
        "github_review_id": review_id,
        "github_review_url": (
            f"https://github.com/kiddhu/hermes-agent/pull/{pr}"
            f"#pullrequestreview-{review_id}"
        ),
        "github_review_state": "APPROVED",
    }


# Canonical caller receipts (what a live gm/gm2 controller supplies after
# mapping the terminal metadata aliases to the closed tool schema).
RECEIPT_4276 = _receipt(HEAD_4276, TREE_4276, BASE_4276, PR_4276, REVIEW_ID_4276)
RECEIPT_4268 = _receipt(
    EXACT_HEAD_4268, TREE_4268, BASE_4268, PR_4268, REVIEW_ID_4268,
)


# Faithful terminal-run metadata copies: run 4276 uses ``verdict`` instead of
# ``review_outcome``; run 4268 uses ``exact_head`` instead of ``head``.  Both
# carry extra non-receipt fields that the parser must ignore.
METADATA_4276 = {
    "verdict": "PASS_EXACT_HEAD",
    "repository": "kiddhu/hermes-agent",
    "pr": PR_4276,
    "head": HEAD_4276,
    "tree": TREE_4276,
    "base": BASE_4276,
    "github_review_id": REVIEW_ID_4276,
    "github_review_url": (
        f"https://github.com/kiddhu/hermes-agent/pull/{PR_4276}"
        f"#pullrequestreview-{REVIEW_ID_4276}"
    ),
    "github_review_state": "APPROVED",
    "github_auditor": "GemAION",
    "github_author": "007AION",
    "role_separation": "PASS",
    "merge_allowed": True,
    "merge_performed": False,
    "worker_session_id": "20260909_175039_7a65e4",
}

METADATA_4268 = {
    "base": BASE_4268,
    "exact_head": EXACT_HEAD_4268,
    "github_review_id": REVIEW_ID_4268,
    "github_review_state": "APPROVED",
    "github_review_url": (
        f"https://github.com/kiddhu/hermes-agent/pull/{PR_4268}"
        f"#pullrequestreview-{REVIEW_ID_4268}"
    ),
    "new_control_plane_count": 0,
    "pr": PR_4268,
    "repository": "kiddhu/hermes-agent",
    "review_outcome": "approved",
    "role_separation": {"author": "007AION", "auditor": "GemAION"},
    "tree": TREE_4268,
    "worker_session_id": "20260909_155707_afdc49",
}


# ---------------------------------------------------------------------------
# Unit tests for the alias normalizer
# ---------------------------------------------------------------------------

def test_normalize_accepts_canonical_spelling():
    assert kb._normalize_completed_pass_recovery_receipt(RECEIPT_4276) == RECEIPT_4276


def test_normalize_accepts_exact_head_alias():
    receipt = copy.deepcopy(RECEIPT_4268)
    del receipt["head"]
    receipt["exact_head"] = EXACT_HEAD_4268
    assert kb._normalize_completed_pass_recovery_receipt(receipt) == RECEIPT_4268


def test_normalize_accepts_verdict_pass_exact_head_alias():
    receipt = copy.deepcopy(RECEIPT_4276)
    del receipt["review_outcome"]
    receipt["verdict"] = "PASS_EXACT_HEAD"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) == RECEIPT_4276


def test_normalize_accepts_both_aliases_together():
    receipt = copy.deepcopy(RECEIPT_4276)
    del receipt["review_outcome"]
    receipt["verdict"] = "PASS_EXACT_HEAD"
    del receipt["head"]
    receipt["exact_head"] = HEAD_4276
    assert kb._normalize_completed_pass_recovery_receipt(receipt) == RECEIPT_4276


def test_normalize_accepts_identical_head_and_exact_head():
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["exact_head"] = HEAD_4276
    assert kb._normalize_completed_pass_recovery_receipt(receipt) == RECEIPT_4276


def test_normalize_accepts_agreeing_review_outcome_and_verdict():
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["verdict"] = "PASS_EXACT_HEAD"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) == RECEIPT_4276


def test_normalize_rejects_conflicting_head_and_exact_head():
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["exact_head"] = "d" * 40
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_conflicting_review_outcome_and_verdict():
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["verdict"] = "PASS_EXACT_HEAD"
    receipt["review_outcome"] = "REQUEST_CHANGES_EXACT_HEAD"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_verdict_against_non_approved_state():
    receipt = copy.deepcopy(RECEIPT_4276)
    del receipt["review_outcome"]
    receipt["verdict"] = "PASS_EXACT_HEAD"
    receipt["github_review_state"] = "CHANGES_REQUESTED"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_unknown_verdict_alias():
    receipt = copy.deepcopy(RECEIPT_4276)
    del receipt["review_outcome"]
    receipt["verdict"] = "APPROVE_EXACT_HEAD"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_missing_both_head_forms():
    receipt = copy.deepcopy(RECEIPT_4276)
    del receipt["head"]
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_missing_both_outcome_forms():
    receipt = copy.deepcopy(RECEIPT_4276)
    del receipt["review_outcome"]
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_non_approved_review_outcome():
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["review_outcome"] = "REQUEST_CHANGES_EXACT_HEAD"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_non_approved_github_state():
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["github_review_state"] = "CHANGES_REQUESTED"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_malformed_head_tree_base_review_id():
    for key, bad in (
        ("head", "not-a-sha"),
        ("head", "f" * 39),
        ("tree", "not-a-sha"),
        ("base", "not-a-sha"),
    ):
        receipt = copy.deepcopy(RECEIPT_4276)
        receipt[key] = bad
        assert kb._normalize_completed_pass_recovery_receipt(receipt) is None
    for key, bad in (
        ("github_review_id", 0),
        ("github_review_id", -1),
        ("pr", 0),
        ("pr", -1),
    ):
        receipt = copy.deepcopy(RECEIPT_4276)
        receipt[key] = bad
        assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_wrong_repository_and_url():
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["repository"] = "attacker/repo"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None
    receipt = copy.deepcopy(RECEIPT_4276)
    receipt["github_review_url"] = "https://example.com/x"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_nested_recovery_receipt_wrapper():
    assert kb._normalize_completed_pass_recovery_receipt(
        {"recovery_receipt": RECEIPT_4276},
    ) is None


def test_normalize_rejects_non_dict():
    for bad in (None, [], "x", 1, True):
        assert kb._normalize_completed_pass_recovery_receipt(bad) is None


# ---------------------------------------------------------------------------
# Full recovery-flow integration tests with copied fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _claim(conn, task_id) -> int:
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    return int(claimed.current_run_id)


def _alias_fixture(conn, *, head, tree, base, pr, terminal_metadata):
    """Build a repeated-audit PASS shape whose terminal run carries aliased
    metadata (copied from the real terminal run) instead of the canonical
    receipt spelling."""
    author = kb.create_task(
        conn, title="implementation", factory_build_gate=1, assignee="agent007",
    )
    audit = kb.create_task(
        conn, title="exact-head audit", assignee="bafuxunan", parents=[author],
    )
    for i in range(3):
        author_run = _claim(conn, author)
        assert kb.request_review_handoff(
            conn, author, expected_run_id=author_run, review_task_id=audit,
            reason=f"PR #{pr} round {i} head {'d' * 40}",
        ) is not None
        audit_run = _claim(conn, audit)
        assert kb.record_review_verdict(
            conn, author, review_task_id=audit,
            expected_review_run_id=audit_run, verdict="request_changes",
            reason=f"REQUEST_CHANGES round {i}",
        )
    author_run = _claim(conn, author)
    assert kb.request_review_handoff(
        conn, author, expected_run_id=author_run, review_task_id=audit,
        reason=f"PR #{pr} frozen at exact head {head}",
    ) is not None
    precursor_run = _claim(conn, audit)
    precursor_reason = (
        f"PASS exact head {head}/tree {tree}/base {base}. Independently "
        "reproduced named base RED tests; changed suites GREEN."
    )
    assert kb.record_review_verdict(
        conn, author, review_task_id=audit,
        expected_review_run_id=precursor_run, verdict="pass",
        reason=precursor_reason,
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status='crashed', outcome='crashed', "
            "summary=NULL, metadata=?, ended_at=11111, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (json.dumps({"pid": 1, "protocol_violation": True}), precursor_run),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (audit,),
        )
    terminal_run = _claim(conn, audit)
    terminal_summary = (
        f"Independent exact-head audit PASS for kiddhu/hermes-agent PR #{pr} "
        f"at head {head}/tree {tree}."
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status='done', outcome='completed', summary=?, "
            "metadata=?, ended_at=22222, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE id=?",
            (terminal_summary, json.dumps(terminal_metadata), terminal_run),
        )
        conn.execute(
            "UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (audit,),
        )
    controller = kb.create_task(conn, title="controller", assignee="gm2")
    controller_run = _claim(conn, controller)
    return {
        "author": author,
        "audit": audit,
        "terminal_run": terminal_run,
        "controller": controller,
        "controller_run": controller_run,
        "terminal_summary": terminal_summary,
    }


def _recover(conn, fixture, receipt, reason):
    return kb.record_review_verdict(
        conn,
        fixture["author"],
        review_task_id=fixture["audit"],
        expected_review_run_id=fixture["terminal_run"],
        verdict="pass",
        reason=reason,
        recovery_receipt=receipt,
        controller_task_id=fixture["controller"],
        controller_run_id=fixture["controller_run"],
        controller_profile="gm2",
    )


def _snapshot(conn):
    return "\n".join(conn.iterdump())


def test_recovery_accepts_verdict_alias_metadata_run4276(kanban_home):
    with kb.connect() as conn:
        fixture = _alias_fixture(
            conn, head=HEAD_4276, tree=TREE_4276, base=BASE_4276,
            pr=PR_4276, terminal_metadata=METADATA_4276,
        )
        before = _snapshot(conn)
        assert _recover(conn, fixture, RECEIPT_4276, fixture["terminal_summary"])
        after = _snapshot(conn)
        assert after != before
        # Idempotent replay writes nothing further.
        assert _recover(conn, fixture, RECEIPT_4276, fixture["terminal_summary"])
        assert _snapshot(conn) == after


def test_recovery_accepts_exact_head_alias_metadata_run4268(kanban_home):
    with kb.connect() as conn:
        fixture = _alias_fixture(
            conn, head=EXACT_HEAD_4268, tree=TREE_4268, base=BASE_4268,
            pr=PR_4268, terminal_metadata=METADATA_4268,
        )
        before = _snapshot(conn)
        assert _recover(conn, fixture, RECEIPT_4268, fixture["terminal_summary"])
        after = _snapshot(conn)
        assert after != before
        assert _recover(conn, fixture, RECEIPT_4268, fixture["terminal_summary"])
        assert _snapshot(conn) == after


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__("head", "f" * 40),          # wrong head vs caller
        lambda m: m.__setitem__("exact_head", "d" * 40),    # head vs exact_head drift
        lambda m: m.__setitem__("verdict", "APPROVE_EXACT_HEAD"),
        lambda m: m.__setitem__("github_review_state", "CHANGES_REQUESTED"),
        lambda m: m.__setitem__("github_review_id", 1),
        lambda m: m.__setitem__("repository", "attacker/repo"),
    ],
)
def test_recovery_rejects_hostile_aliased_metadata(kanban_home, mutate):
    with kb.connect() as conn:
        metadata = copy.deepcopy(METADATA_4276)
        mutate(metadata)
        fixture = _alias_fixture(
            conn, head=HEAD_4276, tree=TREE_4276, base=BASE_4276,
            pr=PR_4276, terminal_metadata=metadata,
        )
        before = _snapshot(conn)
        assert not _recover(conn, fixture, RECEIPT_4276, fixture["terminal_summary"])
        assert _snapshot(conn) == before


def test_recovery_rejects_conflicting_head_and_exact_head_metadata(kanban_home):
    with kb.connect() as conn:
        metadata = copy.deepcopy(METADATA_4268)
        metadata["head"] = "f" * 40  # conflicts with exact_head
        fixture = _alias_fixture(
            conn, head=EXACT_HEAD_4268, tree=TREE_4268, base=BASE_4268,
            pr=PR_4268, terminal_metadata=metadata,
        )
        before = _snapshot(conn)
        assert not _recover(conn, fixture, RECEIPT_4268, fixture["terminal_summary"])
        assert _snapshot(conn) == before


def test_recovery_rejects_conflicting_verdict_and_review_outcome_metadata(kanban_home):
    with kb.connect() as conn:
        metadata = copy.deepcopy(METADATA_4268)
        metadata["verdict"] = "PASS_EXACT_HEAD"  # review_outcome already approved
        metadata["review_outcome"] = "REQUEST_CHANGES_EXACT_HEAD"
        fixture = _alias_fixture(
            conn, head=EXACT_HEAD_4268, tree=TREE_4268, base=BASE_4268,
            pr=PR_4268, terminal_metadata=metadata,
        )
        before = _snapshot(conn)
        assert not _recover(conn, fixture, RECEIPT_4268, fixture["terminal_summary"])
        assert _snapshot(conn) == before
