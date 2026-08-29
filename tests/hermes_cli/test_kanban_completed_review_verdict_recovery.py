"""Fail-closed recovery for an omitted verdict from a completed audit run."""

from __future__ import annotations

import copy
import json
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


SUMMARY = "REQUEST_CHANGES_EXACT_HEAD: two exact blockers"
RECEIPT = {
    "review_outcome": "REQUEST_CHANGES_EXACT_HEAD",
    "repository": "kiddhu/hermes-agent",
    "pr": 60,
    "head": "1" * 40,
    "tree": "2" * 40,
    "base": "3" * 40,
    "github_review_id": 5056888208,
    "github_review_url": (
        "https://github.com/kiddhu/hermes-agent/pull/60"
        "#pullrequestreview-5056888208"
    ),
    "github_review_state": "CHANGES_REQUESTED",
}


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


def _fixture(conn, *, controller_profile="gm2"):
    author = kb.create_task(conn, title="implementation", assignee="agent007")
    author_claim = kb.claim_task(conn, author)
    assert author_claim is not None and author_claim.current_run_id is not None
    audit = kb.create_task(
        conn, title="exact audit", assignee="bafuxunan", parents=[author]
    )
    assert kb.request_review_handoff(
        conn,
        author,
        expected_run_id=author_claim.current_run_id,
        review_task_id=audit,
        reason="PR #60 exact head frozen",
    )
    audit_claim = kb.claim_task(conn, audit)
    assert audit_claim is not None and audit_claim.current_run_id is not None
    audit_run = audit_claim.current_run_id
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status='done', outcome='completed', summary=?, "
            "metadata=?, ended_at=12345, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE id=?",
            (SUMMARY, json.dumps({**RECEIPT, "changed_files": ["x.py"]}), audit_run),
        )
        conn.execute(
            "UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (audit,),
        )
    controller = kb.create_task(
        conn, title="controller", assignee=controller_profile
    )
    controller_claim = kb.claim_task(conn, controller)
    assert controller_claim is not None and controller_claim.current_run_id is not None
    return author, audit, audit_run, controller, controller_claim.current_run_id


def _snapshot(conn):
    return "\n".join(conn.iterdump())


def _recover(conn, fixture, *, receipt=None, profile="gm2", reason=SUMMARY, run_id=None):
    author, audit, audit_run, controller, controller_run = fixture
    return kb.record_review_verdict(
        conn,
        author,
        review_task_id=audit,
        expected_review_run_id=audit_run if run_id is None else run_id,
        verdict="request_changes",
        reason=reason,
        recovery_receipt=RECEIPT if receipt is None else receipt,
        controller_task_id=controller,
        controller_run_id=controller_run,
        controller_profile=profile,
    )


def test_live_auditor_path_cannot_recover_terminal_run_and_does_not_mutate(kanban_home):
    with kb.connect() as conn:
        fixture = _fixture(conn)
        author, audit, audit_run, _, _ = fixture
        before = _snapshot(conn)
        assert not kb.record_review_verdict(
            conn,
            author,
            review_task_id=audit,
            expected_review_run_id=audit_run,
            verdict="request_changes",
            reason=SUMMARY,
        )
        assert _snapshot(conn) == before


def test_completed_recovery_resumes_same_author_once_and_is_idempotent(kanban_home):
    with kb.connect() as conn:
        fixture = _fixture(conn)
        _, audit, audit_run, _, _ = fixture
        audit_before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (audit,)).fetchone())
        run_before = dict(conn.execute("SELECT * FROM task_runs WHERE id=?", (audit_run,)).fetchone())

        assert _recover(conn, fixture)
        assert _recover(conn, fixture)

        author = kb.get_task(conn, fixture[0])
        assert author is not None and author.status == "ready"
        assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (audit,)).fetchone()) == audit_before
        assert dict(conn.execute("SELECT * FROM task_runs WHERE id=?", (audit_run,)).fetchone()) == run_before
        events = conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? "
            "AND kind='review_verdict'",
            (fixture[0],),
        ).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert events[0]["run_id"] == audit_run
        assert payload["recovery"] is True
        assert payload["recovery_receipt"] == RECEIPT
        assert payload["controller"]["profile"] == "gm2"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? "
            "AND kind='review_verdict'",
            (audit,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt.__setitem__("head", "f" * 40),
        lambda receipt: receipt.__setitem__("github_review_id", 1),
        lambda receipt: receipt.__setitem__("github_review_state", "APPROVED"),
        lambda receipt: receipt.__setitem__("repository", "attacker/repo"),
        lambda receipt: receipt.pop("github_review_url"),
        lambda receipt: receipt.__setitem__("extra", "prose"),
    ],
)
def test_recovery_receipt_conflicts_fail_closed_without_mutation(
    kanban_home, mutate
):
    with kb.connect() as conn:
        fixture = _fixture(conn)
        receipt = copy.deepcopy(RECEIPT)
        mutate(receipt)
        before = _snapshot(conn)
        assert not _recover(conn, fixture, receipt=receipt)
        assert _snapshot(conn) == before


@pytest.mark.parametrize("profile", ["agent007", "bafuxunan", "merger", "user"])
def test_recovery_rejects_unauthorized_controller_without_mutation(
    kanban_home, profile
):
    with kb.connect() as conn:
        fixture = _fixture(conn)
        before = _snapshot(conn)
        assert not _recover(conn, fixture, profile=profile)
        assert _snapshot(conn) == before


def test_recovery_rejects_nonlatest_run_and_non_direct_child(kanban_home):
    with kb.connect() as conn:
        fixture = _fixture(conn)
        before = _snapshot(conn)
        assert not _recover(conn, fixture, run_id=fixture[2] - 1)
        assert _snapshot(conn) == before
        conn.execute(
            "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
            (fixture[0], fixture[1]),
        )
        conn.commit()
        before = _snapshot(conn)
        assert not _recover(conn, fixture)
        assert _snapshot(conn) == before


def test_recovery_conflicting_existing_verdict_fails_closed(kanban_home):
    with kb.connect() as conn:
        fixture = _fixture(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                fixture[0],
                "review_verdict",
                {"version": 1, "review_task_id": fixture[1], "verdict": "pass"},
                run_id=fixture[2],
            )
        before = _snapshot(conn)
        assert not _recover(conn, fixture)
        assert _snapshot(conn) == before


def test_recovery_append_fault_rolls_back_author_and_event(kanban_home, monkeypatch):
    with kb.connect() as conn:
        fixture = _fixture(conn)
        before = _snapshot(conn)

        def fail_append(*args, **kwargs):
            raise RuntimeError("injected append fault")

        monkeypatch.setattr(kb, "_append_event", fail_append)
        with pytest.raises(RuntimeError, match="injected append fault"):
            _recover(conn, fixture)
        assert _snapshot(conn) == before


def test_tool_recovery_binds_live_controller_context(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        fixture = _fixture(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", fixture[3])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(fixture[4]))
    monkeypatch.setenv("HERMES_PROFILE", "gm2")
    result = json.loads(kt._handle_review_verdict({
        "author_task_id": fixture[0],
        "review_task_id": fixture[1],
        "expected_review_run_id": fixture[2],
        "verdict": "request_changes",
        "reason": SUMMARY,
        "recovery_receipt": RECEIPT,
    }))
    assert result["ok"] is True
    with kb.connect() as conn:
        author = kb.get_task(conn, fixture[0])
        assert author is not None and author.status == "ready"


def test_cli_recovery_requires_and_uses_dispatcher_controller_context(
    kanban_home, monkeypatch
):
    from hermes_cli import kanban

    with kb.connect() as conn:
        fixture = _fixture(conn)
    args = Namespace(
        author_task_id=fixture[0],
        review_task_id=fixture[1],
        expected_review_run_id=fixture[2],
        reason=SUMMARY,
        recovery_receipt_json=json.dumps(RECEIPT),
    )
    assert kanban._cmd_review_verdict(args) == 1
    monkeypatch.setenv("HERMES_KANBAN_TASK", fixture[3])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(fixture[4]))
    monkeypatch.setenv("HERMES_PROFILE", "gm2")
    assert kanban._cmd_review_verdict(args) == 0
    with kb.connect() as conn:
        author = kb.get_task(conn, fixture[0])
        assert author is not None and author.status == "ready"
