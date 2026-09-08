"""Fail-closed recovery for an omitted PASS from a completed audit run.

Reproduces the exact generic shape from t_68b8add9 runs 4185/4186: a terminal
auditor run completed with an APPROVED receipt after a same-child predecessor
run emitted the identical version-1 PASS and then crashed from a proven
protocol-violation retry, leaving the author's verdict bound to the wrong
(crashed) run.  A live gm/gm2 controller may bind exactly one omitted PASS to
the latest terminal run; every drift, role, receipt, history, and fault case
must fail closed with byte-identical logical state.
"""

from __future__ import annotations

import copy
import json
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


HEAD = "a" * 40
TREE = "b" * 40
BASE = "c" * 40
REVIEW_ID = 5135670244
PR = 90

PASS_RECEIPT = {
    "review_outcome": "approved",
    "repository": "kiddhu/hermes-agent",
    "pr": PR,
    "head": HEAD,
    "tree": TREE,
    "base": BASE,
    "github_review_id": REVIEW_ID,
    "github_review_url": (
        f"https://github.com/kiddhu/hermes-agent/pull/{PR}"
        f"#pullrequestreview-{REVIEW_ID}"
    ),
    "github_review_state": "APPROVED",
}

PRECURSOR_REASON = (
    f"PASS exact head {HEAD}/tree {TREE}/base {BASE}. Independently "
    "reproduced 4/4 named base RED tests; 216/216 changed suites GREEN."
)
TERMINAL_SUMMARY = (
    f"Independent exact-head audit PASS for kiddhu/hermes-agent PR #{PR} at "
    f"head {HEAD}/tree {TREE}. Revalidated 216/216 focused tests."
)


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


def _pass_fixture(conn, *, controller_profile="gm2"):
    author = kb.create_task(
        conn, title="implementation", factory_build_gate=1, assignee="agent007",
    )
    author_run = _claim(conn, author)
    audit = kb.create_task(
        conn, title="exact-head audit",
        assignee="bafuxunan", parents=[author],
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
    # Predecessor run crashes as a protocol violation; the child is re-claimed
    # by the dispatcher and the terminal run completes with an APPROVED receipt
    # (no second verdict emitted).
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
            (TERMINAL_SUMMARY, json.dumps({**PASS_RECEIPT, "worker_session_id": "s"}),
             terminal_run),
        )
        conn.execute(
            "UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (audit,),
        )
    controller = kb.create_task(conn, title="controller", assignee=controller_profile)
    controller_run = _claim(conn, controller)
    return {
        "author": author,
        "author_run": author_run,
        "audit": audit,
        "precursor_run": precursor_run,
        "terminal_run": terminal_run,
        "controller": controller,
        "controller_run": controller_run,
    }


def _recover_pass(conn, fixture, *, receipt=None, profile="gm2", reason=TERMINAL_SUMMARY,
                  run_id=None, verdict="pass"):
    return kb.record_review_verdict(
        conn,
        fixture["author"],
        review_task_id=fixture["audit"],
        expected_review_run_id=fixture["terminal_run"] if run_id is None else run_id,
        verdict=verdict,
        reason=reason,
        recovery_receipt=PASS_RECEIPT if receipt is None else receipt,
        controller_task_id=fixture["controller"],
        controller_run_id=fixture["controller_run"],
        controller_profile=profile,
    )


def _snapshot(conn):
    return "\n".join(conn.iterdump())


def _recovered_verdicts(conn, fixture):
    return conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id=? "
        "AND kind='review_verdict' ORDER BY id",
        (fixture["author"],),
    ).fetchall()


def test_pass_recovery_emits_one_latest_run_pass_and_is_idempotent(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        before = _snapshot(conn)
        assert _recover_pass(conn, fixture)
        after = _snapshot(conn)
        assert after != before
        assert _recover_pass(conn, fixture)
        assert _snapshot(conn) == after

        # Author stays nonterminal in ``review`` (PASS never resumes).
        author = kb.get_task(conn, fixture["author"])
        assert author is not None and author.status == "review"
        audit = kb.get_task(conn, fixture["audit"])
        assert audit is not None and audit.status == "done"

        rows = _recovered_verdicts(conn, fixture)
        assert len(rows) == 2
        precursor, recovery = rows
        precursor_payload = json.loads(precursor["payload"])
        assert precursor_payload["version"] == 1
        assert precursor_payload["verdict"] == "pass"
        assert precursor_payload["review_run_id"] == fixture["precursor_run"]
        recovery_payload = json.loads(recovery["payload"])
        assert recovery_payload["version"] == 2
        assert recovery_payload["verdict"] == "pass"
        assert recovery_payload["recovery"] is True
        assert recovery_payload["review_run_id"] == fixture["terminal_run"]
        assert recovery_payload["recovery_receipt"] == PASS_RECEIPT
        assert recovery_payload["controller"]["profile"] == "gm2"
        assert recovery["run_id"] == fixture["terminal_run"]

        # A byte-identical mirror is emitted on the audit child.
        mirrors = conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? "
            "AND kind='review_verdict' AND run_id=? ORDER BY id",
            (fixture["audit"], fixture["terminal_run"]),
        ).fetchall()
        assert len(mirrors) == 1
        assert json.loads(mirrors[0]["payload"]) == recovery_payload


def test_resolver_accepts_recovered_pass_binding(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        assert kb._canonical_audit_receipt(conn, fixture["author"]) is None
        assert _recover_pass(conn, fixture)
        receipt = kb._canonical_audit_receipt(conn, fixture["author"])
        assert receipt is not None
        assert receipt["authenticated"] is True
        assert receipt["verdict"] == "PASS"
        assert receipt["auditor_task_id"] == fixture["audit"]
        assert receipt["auditor_run_id"] == fixture["terminal_run"]
        assert receipt["author_run_id"] == fixture["author_run"]


def test_resolver_rejects_recovered_pass_on_receipt_drift(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        # Drift the terminal receipt head, then recover (recovery refuses).
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({**PASS_RECEIPT, "head": "f" * 40}),
                 fixture["terminal_run"]),
            )
        assert not _recover_pass(conn, fixture)
        assert kb._canonical_audit_receipt(conn, fixture["author"]) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.__setitem__("head", "f" * 40),
        lambda r: r.__setitem__("tree", "e" * 40),
        lambda r: r.__setitem__("base", "d" * 40),
        lambda r: r.__setitem__("github_review_id", 1),
        lambda r: r.__setitem__("github_review_state", "CHANGES_REQUESTED"),
        lambda r: r.__setitem__("review_outcome", "REQUEST_CHANGES_EXACT_HEAD"),
        lambda r: r.__setitem__("repository", "attacker/repo"),
        lambda r: r.__setitem__("pr", 999),
        lambda r: r.__setitem__("github_review_url", "https://example.com/x"),
        lambda r: r.pop("github_review_url"),
        lambda r: r.__setitem__("extra", "prose"),
    ],
)
def test_pass_recovery_rejects_receipt_drift_without_mutation(kanban_home, mutate):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        receipt = copy.deepcopy(PASS_RECEIPT)
        mutate(receipt)
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture, receipt=receipt)
        assert _snapshot(conn) == before


@pytest.mark.parametrize("profile", ["agent007", "bafuxunan", "merger", "user"])
def test_pass_recovery_rejects_unauthorized_controller(kanban_home, profile):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture, profile=profile)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_nonlatest_run_and_non_direct_child(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture, run_id=fixture["precursor_run"])
        assert _snapshot(conn) == before
        conn.execute(
            "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
            (fixture["author"], fixture["audit"]),
        )
        conn.commit()
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_non_protocol_violation_predecessor(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        # Predecessor crashed but is NOT a protocol violation.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"pid": 1, "protocol_violation": False}),
                 fixture["precursor_run"]),
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_open_terminal_run(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status='running', outcome=NULL, ended_at=NULL "
                "WHERE id=?", (fixture["terminal_run"],),
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_caller_reason_drift(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture, reason="controller prose")
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_predecessor_reason_commit_drift(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        # Rewrite the predecessor PASS reason so it no longer bears the exact
        # terminal head/tree/base commit identity.
        rows = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id=? AND kind=? "
            "AND run_id=? ORDER BY id",
            (fixture["author"], "review_verdict", fixture["precursor_run"]),
        ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        payload["reason"] = f"PASS exact head {'f' * 40}/tree {'e' * 40}/base {'d' * 40}"
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), rows[0]["id"]),
        )
        conn.commit()
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_self_audit_role_collision(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        conn.execute(
            "UPDATE tasks SET assignee=? WHERE id=?",
            ("agent007", fixture["audit"]),
        )
        conn.commit()
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_author_not_in_review(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=?", (fixture["author"],),
        )
        conn.commit()
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_prior_request_changes_verdict(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        with kb.write_txn(conn):
            kb._append_event(
                conn, fixture["author"], "review_verdict",
                {"version": 1, "review_task_id": fixture["audit"],
                 "review_run_id": fixture["precursor_run"],
                 "verdict": "request_changes", "reason": "conflicting"},
                run_id=fixture["precursor_run"],
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_rejects_intervening_conflicting_verdict(kanban_home):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        # An extra pass bound to a different run for the same auditor.
        with kb.write_txn(conn):
            kb._append_event(
                conn, fixture["author"], "review_verdict",
                {"version": 1, "review_task_id": fixture["audit"],
                 "review_run_id": fixture["terminal_run"],
                 "verdict": "pass", "reason": "ambiguous"},
                run_id=fixture["terminal_run"],
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_pass_recovery_append_fault_rolls_back(kanban_home, monkeypatch):
    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
        before = _snapshot(conn)

        def fail_append(*args, **kwargs):
            raise RuntimeError("injected append fault")

        monkeypatch.setattr(kb, "_append_event", fail_append)
        with pytest.raises(RuntimeError, match="injected append fault"):
            _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_tool_pass_recovery_binds_live_controller(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", fixture["controller"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(fixture["controller_run"]))
    monkeypatch.setenv("HERMES_PROFILE", "gm2")
    result = json.loads(kt._handle_review_verdict({
        "author_task_id": fixture["author"],
        "review_task_id": fixture["audit"],
        "expected_review_run_id": fixture["terminal_run"],
        "verdict": "pass",
        "reason": TERMINAL_SUMMARY,
        "recovery_receipt": PASS_RECEIPT,
    }))
    assert result["ok"] is True
    assert result["task_id"] == fixture["controller"]
    with kb.connect() as conn:
        assert kb._canonical_audit_receipt(conn, fixture["author"]) is not None


def test_cli_pass_recovery_requires_and_uses_dispatcher_context(kanban_home, monkeypatch):
    from hermes_cli import kanban

    with kb.connect() as conn:
        fixture = _pass_fixture(conn)
    args = Namespace(
        author_task_id=fixture["author"],
        review_task_id=fixture["audit"],
        expected_review_run_id=fixture["terminal_run"],
        reason=TERMINAL_SUMMARY,
        recovery_receipt_json=json.dumps(PASS_RECEIPT),
        verdict="pass",
    )
    assert kanban._cmd_review_verdict(args) == 1
    monkeypatch.setenv("HERMES_KANBAN_TASK", fixture["controller"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(fixture["controller_run"]))
    monkeypatch.setenv("HERMES_PROFILE", "gm2")
    assert kanban._cmd_review_verdict(args) == 0
    with kb.connect() as conn:
        assert kb._canonical_audit_receipt(conn, fixture["author"]) is not None
