"""Fail-closed recovery of an omitted PASS from a repeated-audit lineage.

Reproduces the canonical t_d7a8d4dc seven-run shape: a single direct auditor
child is handed off to six times — five earlier rounds each end
REQUEST_CHANGES, then the immediate predecessor run emits a version-1 PASS and
crashes from a proven protocol-violation retry, and a terminal recovery run
completes with an APPROVED receipt and no second verdict.  The installed
completed-audit PASS recovery authenticator historically required exactly two
closed auditor runs, so it rejected this repeated-audit lineage and the exact
merge gate stayed fail-closed.

After the repair, a live gm/gm2 controller may bind exactly one omitted PASS to
the latest terminal run when the *immediate* predecessor is the crashed
version-1 PASS run; older closed request-changes rounds may exist but must
never supply authority, and any stale/non-immediate PASS, multiple/conflicting
verdict, wrong commit identity, receipt drift, or duplicate replay must fail
closed or stay idempotent.
"""

from __future__ import annotations

import copy
import json
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


def _repeated_audit_fixture(conn, *, controller_profile="gm2", rounds=5):
    """Build the canonical seven-run repeated-audit PASS-recovery shape.

    ``rounds`` earlier rounds each end REQUEST_CHANGES; then the immediate
    predecessor run emits a version-1 PASS and crashes from a proven
    protocol-violation retry; the terminal recovery run completes with an
    APPROVED receipt and no second verdict.
    """
    author = kb.create_task(
        conn, title="implementation", factory_build_gate=1, assignee="agent007",
    )
    audit = kb.create_task(
        conn, title="exact-head audit", assignee="bafuxunan", parents=[author],
    )
    for i in range(rounds):
        author_run = _claim(conn, author)
        assert kb.request_review_handoff(
            conn, author, expected_run_id=author_run, review_task_id=audit,
            reason=f"PR #{PR} round {i} head {'d' * 40}",
        ) is not None
        audit_run = _claim(conn, audit)
        assert kb.record_review_verdict(
            conn, author, review_task_id=audit,
            expected_review_run_id=audit_run, verdict="request_changes",
            reason=f"REQUEST_CHANGES round {i}",
        )
    # Final handoff -> immediate predecessor emits the version-1 PASS.
    author_run = _claim(conn, author)
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
    # The predecessor crashes as a protocol violation and the child is
    # re-claimed; the terminal run completes with an APPROVED receipt (no
    # second verdict emitted).
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
        "audit": audit,
        "precursor_run": precursor_run,
        "terminal_run": terminal_run,
        "controller": controller,
        "controller_run": controller_run,
    }


def _recover_pass(conn, fixture, *, receipt=None, profile="gm2",
                  reason=TERMINAL_SUMMARY, run_id=None, verdict="pass"):
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


def _audit_run_ids(conn, fixture):
    return [
        int(row["id"]) for row in conn.execute(
            "SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC",
            (fixture["audit"],),
        ).fetchall()
    ]


def test_repeated_audit_pass_recovery_emits_one_latest_run_pass_and_is_idempotent(kanban_home):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn)
        # Canonical seven-run shape: terminal + crashed predecessor + five older
        # closed request-changes rounds.
        assert len(_audit_run_ids(conn, fixture)) == 7
        before = _snapshot(conn)
        assert _recover_pass(conn, fixture)
        after = _snapshot(conn)
        assert after != before
        assert _recover_pass(conn, fixture)
        assert _snapshot(conn) == after

        author = kb.get_task(conn, fixture["author"])
        assert author is not None and author.status == "review"
        audit = kb.get_task(conn, fixture["audit"])
        assert audit is not None and audit.status == "done"

        rows = _recovered_verdicts(conn, fixture)
        # Five request-changes + one version-1 PASS + one version-2 recovery PASS.
        assert len(rows) == 7
        precursor_rows = [
            r for r in rows if int(r["run_id"]) == fixture["precursor_run"]
        ]
        assert len(precursor_rows) == 1
        precursor = json.loads(precursor_rows[0]["payload"])
        assert precursor["version"] == 1
        assert precursor["verdict"] == "pass"
        assert precursor["review_run_id"] == fixture["precursor_run"]
        recovery_rows = [
            r for r in rows if int(r["run_id"]) == fixture["terminal_run"]
        ]
        assert len(recovery_rows) == 1
        recovery = json.loads(recovery_rows[0]["payload"])
        assert recovery["version"] == 2
        assert recovery["verdict"] == "pass"
        assert recovery["recovery"] is True
        assert recovery["review_run_id"] == fixture["terminal_run"]
        assert recovery["recovery_receipt"] == PASS_RECEIPT
        assert recovery["controller"]["profile"] == "gm2"
        assert recovery_rows[0]["run_id"] == fixture["terminal_run"]

        # A byte-identical mirror is emitted on the audit child.
        mirrors = conn.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id=? "
            "AND kind='review_verdict' AND run_id=? ORDER BY id",
            (fixture["audit"], fixture["terminal_run"]),
        ).fetchall()
        assert len(mirrors) == 1
        assert json.loads(mirrors[0]["payload"]) == recovery


def test_repeated_audit_recovery_rejects_stale_non_immediate_pass(kanban_home):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn)
        # A version-1 PASS bound to an OLDER (non-immediate) run must not
        # authenticate.  Replace one older request-changes verdict's payload
        # with a PASS and drop its mirror to a distinct run.
        older_run_ids = _audit_run_ids(conn, fixture)[2:]  # runs older than predecessor
        stale_run = older_run_ids[0]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload=? WHERE task_id=? "
                "AND kind='review_verdict' AND run_id=?",
                (json.dumps({
                    "version": 1, "review_task_id": fixture["audit"],
                    "review_run_id": stale_run, "verdict": "pass",
                    "reason": PRECURSOR_REASON,
                }), fixture["author"], stale_run),
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_repeated_audit_recovery_rejects_intervening_conflicting_verdict(kanban_home):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn)
        # A conflicting verdict bound to the terminal run itself must reject.
        with kb.write_txn(conn):
            kb._append_event(
                conn, fixture["author"], "review_verdict",
                {"version": 1, "review_task_id": fixture["audit"],
                 "review_run_id": fixture["terminal_run"],
                 "verdict": "request_changes", "reason": "conflicting"},
                run_id=fixture["terminal_run"],
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_repeated_audit_recovery_rejects_second_predecessor_pass(kanban_home):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn)
        # A second PASS bound to the immediate predecessor would make the
        # predecessor ambiguous; it must reject.
        with kb.write_txn(conn):
            kb._append_event(
                conn, fixture["author"], "review_verdict",
                {"version": 1, "review_task_id": fixture["audit"],
                 "review_run_id": fixture["precursor_run"],
                 "verdict": "pass", "reason": PRECURSOR_REASON},
                run_id=fixture["precursor_run"],
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


def test_repeated_audit_recovery_rejects_non_protocol_violation_predecessor(kanban_home):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"pid": 1, "protocol_violation": False}),
                 fixture["precursor_run"]),
            )
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture)
        assert _snapshot(conn) == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.__setitem__("head", "f" * 40),
        lambda r: r.__setitem__("tree", "e" * 40),
        lambda r: r.__setitem__("base", "d" * 40),
        lambda r: r.__setitem__("github_review_id", 1),
        lambda r: r.__setitem__("github_review_state", "CHANGES_REQUESTED"),
        lambda r: r.__setitem__("review_outcome", "REQUEST_CHANGES_EXACT_HEAD"),
        lambda r: r.pop("github_review_url"),
    ],
)
def test_repeated_audit_recovery_rejects_receipt_drift(kanban_home, mutate):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn)
        receipt = copy.deepcopy(PASS_RECEIPT)
        mutate(receipt)
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture, receipt=receipt)
        assert _snapshot(conn) == before


def test_repeated_audit_recovery_rejects_caller_reason_drift(kanban_home):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn)
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture, reason="controller prose")
        assert _snapshot(conn) == before


def test_repeated_audit_recovery_rejects_unauthorized_controller(kanban_home):
    with kb.connect() as conn:
        fixture = _repeated_audit_fixture(conn, controller_profile="agent007")
        before = _snapshot(conn)
        assert not _recover_pass(conn, fixture, profile="agent007")
        assert _snapshot(conn) == before
