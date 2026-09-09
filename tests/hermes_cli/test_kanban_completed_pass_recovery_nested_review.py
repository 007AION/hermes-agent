"""Nested ``github_review`` compatibility for completed-audit PASS recovery.

Reproduces the exact generic shape from t_25563053 runs 4288/4289 (PR #96): a
terminal auditor run completed with an APPROVED receipt, but the review
identity was persisted in a nested ``github_review.{id,url,state,author,
commit_id}`` object instead of the canonical top-level
``github_review_id/url/state`` spelling the installed normalizer required.

The normalizer must lift one complete nested object to the canonical form
only when it is unique (no top-level review-id/url/state key — otherwise
conflict), typed (exactly the five keys), non-conflicting (``commit_id`` is
byte-equal to ``head`` and ``author`` is the factory auditor actor), and
fully corroborated (``state`` APPROVED and ``url`` matches the exact
commit-bound GitHub review URL for the terminal PR/id).  Every partial,
malformed, unknown-key, wrong-identity, wrong-commit, non-APPROVED, or
nested+top-level-conflict shape must fail closed with zero mutation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# Exact commit-bound identity copied from the live terminal run (read-only):
# PR #96 head d0cd38d4..., tree f6050f4c..., base 13d9faad..., GitHub review
# 5154659342 APPROVED by GemAION at commit d0cd38d4....
HEAD = "d0cd38d4f87f2e77dd92fd5faa358d1d67d60286"
TREE = "f6050f4cad2e89e7fefd5d049c103e848142ec91"
BASE = "13d9faadb3cf1215888a59d9911c5b8e8a2114df"
PR = 96
REVIEW_ID = 5154659342
REVIEW_URL = (
    f"https://github.com/kiddhu/hermes-agent/pull/{PR}"
    f"#pullrequestreview-{REVIEW_ID}"
)


def _canonical_receipt():
    return {
        "review_outcome": "approved",
        "repository": "kiddhu/hermes-agent",
        "pr": PR,
        "head": HEAD,
        "tree": TREE,
        "base": BASE,
        "github_review_id": REVIEW_ID,
        "github_review_url": REVIEW_URL,
        "github_review_state": "APPROVED",
    }


# Faithful terminal-run metadata copy: review identity nested under
# ``github_review`` (author/commit_id/id/state/url) plus extra non-receipt
# fields that the parser must ignore.
NESTED_METADATA = {
    "review_outcome": "approved",
    "repository": "kiddhu/hermes-agent",
    "pr": PR,
    "head": HEAD,
    "tree": TREE,
    "base": BASE,
    "github_review": {
        "author": "GemAION",
        "commit_id": HEAD,
        "id": REVIEW_ID,
        "state": "APPROVED",
        "url": REVIEW_URL,
    },
    "live_readback": {"merge_state": "CLEAN", "mergeable": "MERGEABLE",
                       "pr_state": "OPEN", "required_checks": "pass"},
    "scope_constraints": {"merge_performed": False, "install_performed": False},
    "worker_session_id": "20260909_210447_1372bf",
}


# ---------------------------------------------------------------------------
# Unit tests for the nested-review normalizer
# ---------------------------------------------------------------------------

def test_normalize_accepts_nested_github_review():
    assert kb._normalize_completed_pass_recovery_receipt(NESTED_METADATA) == (
        _canonical_receipt()
    )


def test_normalize_nested_result_is_canonical_top_level_shape():
    result = kb._normalize_completed_pass_recovery_receipt(NESTED_METADATA)
    assert result is not None
    assert set(result) == kb._COMPLETED_RECOVERY_RECEIPT_KEYS
    # No nested object survives; the review identity is top-level canonical.
    assert "github_review" not in result
    assert result["github_review_id"] == REVIEW_ID
    assert result["github_review_url"] == REVIEW_URL
    assert result["github_review_state"] == "APPROVED"


def test_normalize_rejects_nested_top_level_conflict():
    for key in ("github_review_id", "github_review_url", "github_review_state"):
        receipt = copy.deepcopy(NESTED_METADATA)
        receipt[key] = (
            REVIEW_ID if key == "github_review_id"
            else REVIEW_URL if key == "github_review_url"
            else "APPROVED"
        )
        assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_null_shadow_top_level_conflict():
    for key in ("github_review_id", "github_review_url", "github_review_state"):
        receipt = copy.deepcopy(NESTED_METADATA)
        receipt[key] = None
        assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


@pytest.mark.parametrize("missing", ["author", "commit_id", "id", "state", "url"])
def test_normalize_rejects_missing_nested_key(missing):
    receipt = copy.deepcopy(NESTED_METADATA)
    del receipt["github_review"][missing]
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_unknown_nested_key():
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"]["extra"] = "prose"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_wrong_nested_author():
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"]["author"] = "007AION"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_wrong_nested_commit_id():
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"]["commit_id"] = "f" * 40
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_malformed_nested_commit_id():
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"]["commit_id"] = "not-a-sha"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_commit_id_drift_from_head():
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["head"] = "f" * 40  # head drift vs nested commit_id
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


@pytest.mark.parametrize("bad", [0, -1, True, "x", None])
def test_normalize_rejects_bad_nested_id(bad):
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"]["id"] = bad
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


@pytest.mark.parametrize("bad", ["CHANGES_REQUESTED", "PENDING", None, 1])
def test_normalize_rejects_bad_nested_state(bad):
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"]["state"] = bad
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


@pytest.mark.parametrize("bad", ["https://example.com/x", None, 1])
def test_normalize_rejects_bad_nested_url(bad):
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"]["url"] = bad
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_nested_wrong_pr():
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["pr"] = 999
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_nested_wrong_repository():
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["repository"] = "attacker/repo"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


@pytest.mark.parametrize("field", ["head", "tree", "base"])
def test_normalize_rejects_nested_malformed_commit_fields(field):
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt[field] = "not-a-sha"
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


@pytest.mark.parametrize("bad", [None, "x", [], 1, True])
def test_normalize_rejects_non_dict_nested_github_review(bad):
    receipt = copy.deepcopy(NESTED_METADATA)
    receipt["github_review"] = bad
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


def test_normalize_rejects_absent_nested_and_top_level_review():
    receipt = copy.deepcopy(NESTED_METADATA)
    del receipt["github_review"]
    assert kb._normalize_completed_pass_recovery_receipt(receipt) is None


# ---------------------------------------------------------------------------
# Full recovery-flow integration tests with the copied nested fixture
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


PRECURSOR_REASON = (
    f"PASS exact head {HEAD}/tree {TREE}/base {BASE}. Independently "
    "reproduced named base RED tests; changed suites GREEN."
)
TERMINAL_SUMMARY = (
    f"Independent exact-head audit PASS for kiddhu/hermes-agent PR #{PR} at "
    f"head {HEAD}/tree {TREE}. Revalidated focused tests."
)


def _nested_fixture(conn, *, terminal_metadata):
    author = kb.create_task(
        conn, title="implementation", factory_build_gate=1, assignee="agent007",
    )
    author_run = _claim(conn, author)
    audit = kb.create_task(
        conn, title="exact-head audit", assignee="bafuxunan", parents=[author],
    )
    assert kb.request_review_handoff(
        conn, author, expected_run_id=author_run, review_task_id=audit,
        reason=f"PR #{PR} frozen at exact head {HEAD}",
    ) is not None
    precursor_run = _claim(conn, audit)
    assert kb.record_review_verdict(
        conn, author, review_task_id=audit,
        expected_review_run_id=precursor_run, verdict="pass",
        reason=PRECURSOR_REASON,
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
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status='done', outcome='completed', summary=?, "
            "metadata=?, ended_at=22222, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE id=?",
            (TERMINAL_SUMMARY, json.dumps(terminal_metadata), terminal_run),
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
        "author_run": author_run,
        "audit": audit,
        "terminal_run": terminal_run,
        "controller": controller,
        "controller_run": controller_run,
    }


def _recover(conn, fixture, receipt):
    return kb.record_review_verdict(
        conn,
        fixture["author"],
        review_task_id=fixture["audit"],
        expected_review_run_id=fixture["terminal_run"],
        verdict="pass",
        reason=TERMINAL_SUMMARY,
        recovery_receipt=receipt,
        controller_task_id=fixture["controller"],
        controller_run_id=fixture["controller_run"],
        controller_profile="gm2",
    )


def _snapshot(conn):
    return "\n".join(conn.iterdump())


def test_recovery_accepts_nested_review_terminal_metadata(kanban_home):
    with kb.connect() as conn:
        fixture = _nested_fixture(conn, terminal_metadata=NESTED_METADATA)
        before = _snapshot(conn)
        assert _recover(conn, fixture, _canonical_receipt())
        after = _snapshot(conn)
        assert after != before
        # Idempotent replay writes nothing further.
        assert _recover(conn, fixture, _canonical_receipt())
        assert _snapshot(conn) == after
        # Emitted recovery receipt is the canonical top-level schema.
        rows = conn.execute(
            "SELECT id, run_id, payload FROM task_events WHERE task_id=? "
            "AND kind='review_verdict' ORDER BY id",
            (fixture["author"],),
        ).fetchall()
        assert len(rows) == 2
        recovery_payload = json.loads(rows[1]["payload"])
        assert recovery_payload["recovery_receipt"] == _canonical_receipt()
        assert "github_review" not in recovery_payload["recovery_receipt"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__("head", "f" * 40),
        lambda m: m.__setitem__("tree", "e" * 40),
        lambda m: m.__setitem__("base", "d" * 40),
        lambda m: m.__setitem__("repository", "attacker/repo"),
        lambda m: m["github_review"].__setitem__("author", "007AION"),
        lambda m: m["github_review"].__setitem__("commit_id", "f" * 40),
        lambda m: m["github_review"].__setitem__("state", "CHANGES_REQUESTED"),
        lambda m: m["github_review"].__setitem__("id", 1),
        lambda m: m["github_review"].__setitem__("url", "https://example.com/x"),
        lambda m: m["github_review"].pop("id"),
        lambda m: m["github_review"].__setitem__("extra", "prose"),
    ],
)
def test_recovery_rejects_hostile_nested_metadata(kanban_home, mutate):
    with kb.connect() as conn:
        metadata = copy.deepcopy(NESTED_METADATA)
        mutate(metadata)
        fixture = _nested_fixture(conn, terminal_metadata=metadata)
        before = _snapshot(conn)
        assert not _recover(conn, fixture, _canonical_receipt())
        assert _snapshot(conn) == before
