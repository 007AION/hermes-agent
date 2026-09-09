"""Canonical audit-outcome envelope + atomic continuation invariant.

Reproduces the split-PASS defect (PASS -> stop-guard protocol-violation crash
-> successor-run completion -> ``_canonical_audit_receipt`` null) as base RED,
then proves the corrected producer path: a bound PASS completed in the *same*
run binds exactly one version-3 canonical audit-outcome envelope, one bounded
``changed_fact``, and exactly one ``FINAL_ACCEPTED`` xor
``CONTINUATION_COMMITTED`` disposition, atomically inside the terminal writer.

Hostile identity / evidence / role / continuation matrices must fail closed
with zero mutation, and the resolved envelope must be what the reviewed-author
finalizer consumes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# Exact closed evidence identity (the same canonical spelling the PASS
# recovery normalizer already accepts).
REPOSITORY = "kiddhu/hermes-agent"
PR = 97
HEAD = "d0cd38d4f87f2e77dd92fd5faa358d1d67d60286"
TREE = "f6050f4cad2e89e7fefd5d049c103e848142ec91"
BASE = "13d9faadb3cf1215888a59d9911c5b8e8a2114df"
REVIEW_ID = 5154659342
REVIEW_URL = (
    f"https://github.com/{REPOSITORY}/pull/{PR}"
    f"#pullrequestreview-{REVIEW_ID}"
)

AUTHOR_PROFILE = "agent007"
AUDITOR_PROFILE = "bafuxunan"


def _evidence_metadata():
    return {
        "review_outcome": "approved",
        "repository": REPOSITORY,
        "pr": PR,
        "head": HEAD,
        "tree": TREE,
        "base": BASE,
        "github_review_id": REVIEW_ID,
        "github_review_url": REVIEW_URL,
        "github_review_state": "APPROVED",
    }


def _evidence_block():
    """The closed 8-key evidence block the auditor binds to a PASS verdict."""
    return {
        "repository": REPOSITORY,
        "pr": PR,
        "head": HEAD,
        "tree": TREE,
        "base": BASE,
        "github_review_id": REVIEW_ID,
        "github_review_url": REVIEW_URL,
        "github_review_state": "APPROVED",
    }


PRECURSOR_REASON = (
    f"PASS exact head {HEAD}/tree {TREE}/base {BASE}. Independently "
    "reproduced named base RED tests; changed suites GREEN."
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


def _fixture(conn, *, continuation_child: bool = False):
    """Author (agent007) -> audit (bafuxunan), PASS bound to the audit run."""
    author = kb.create_task(
        conn, title="implementation", factory_build_gate=1, assignee=AUTHOR_PROFILE,
    )
    author_run = _claim(conn, author)
    audit = kb.create_task(
        conn, title="exact-head audit", assignee=AUDITOR_PROFILE, parents=[author],
    )
    if continuation_child:
        kb.create_task(
            conn, title="merge", assignee="merger", parents=[author],
        )
    assert kb.request_review_handoff(
        conn, author, expected_run_id=author_run, review_task_id=audit,
        reason=f"PR #{PR} frozen at exact head {HEAD}",
    ) is not None
    audit_run = _claim(conn, audit)
    assert kb.record_review_verdict(
        conn, author, review_task_id=audit,
        expected_review_run_id=audit_run, verdict="pass",
        reason=PRECURSOR_REASON,
        evidence=_evidence_block(),
    )
    return {
        "author": author,
        "author_run": author_run,
        "audit": audit,
        "audit_run": audit_run,
    }


def _snapshot(conn):
    return "\n".join(conn.iterdump())


def _events(conn, task_id, kind):
    return conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (task_id, kind),
    ).fetchall()


# ---------------------------------------------------------------------------
# Base RED: split PASS -> protocol-violation crash -> successor completion
# leaves the canonical receipt null (the defect the producer repair removes).
# ---------------------------------------------------------------------------

def test_base_red_split_pass_null_receipt(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn)
        # Simulate the old stop-guard: PASS then the worker exits -> crashed.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status='crashed', outcome='crashed', "
                "summary=NULL, metadata=?, ended_at=11111, claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (json.dumps({"pid": 1, "protocol_violation": True}), fx["audit_run"]),
            )
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=NULL, "
                "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (fx["audit"],),
            )
        terminal_run = _claim(conn, fx["audit"])
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status='done', outcome='completed', "
                "summary=?, metadata=?, ended_at=22222, claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (PRECURSOR_REASON, json.dumps(_evidence_metadata()), terminal_run),
            )
            conn.execute(
                "UPDATE tasks SET status='done', current_run_id=NULL, "
                "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (fx["audit"],),
            )
        # The verdict and the terminal evidence are bound to different runs.
        assert kb._canonical_audit_receipt(conn, fx["author"]) is None
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


# ---------------------------------------------------------------------------
# GREEN: a bound PASS completed in the same run binds one canonical envelope.
# ---------------------------------------------------------------------------

def test_green_binds_canonical_envelope(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn)
        assert kb.complete_task(
            conn, fx["audit"], expected_run_id=fx["audit_run"],
            metadata=_evidence_metadata(),
        )
        envelope = json.loads(
            _events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT)[0]["payload"]
        )
        assert envelope["version"] == kb.CANONICAL_AUDIT_OUTCOME_VERSION
        assert envelope["author_task_id"] == fx["author"]
        assert envelope["author_run_id"] == fx["author_run"]
        assert envelope["audit_task_id"] == fx["audit"]
        assert envelope["audit_run_id"] == fx["audit_run"]
        assert envelope["verdict"] == "PASS"
        assert envelope["reason"] == PRECURSOR_REASON
        assert envelope["evidence"]["repository"] == REPOSITORY
        assert envelope["role_separation"] == {
            "author_profile": AUTHOR_PROFILE,
            "auditor_profile": AUDITOR_PROFILE,
        }
        # Envelope is persisted on both the audit and the author.
        assert len(_events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT)) == 1
        assert len(_events(conn, fx["author"], kb.CANONICAL_AUDIT_OUTCOME_EVENT)) == 1
        # Bounded changed_fact carries the disposition + continuation + digest.
        facts = _events(conn, fx["audit"], kb.CHANGED_FACT_EVENT)
        assert len(facts) == 1
        changed = json.loads(facts[0]["payload"])
        assert changed["task_id"] == fx["audit"]
        assert changed["run_id"] == fx["audit_run"]
        assert changed["prior_status"] == "running"
        assert changed["new_status"] == "done"
        assert changed["audit_outcome_sha256"] == envelope["evidence_sha256"]
        assert changed["disposition"] in (
            kb.AUDIT_DISPOSITION_FINAL_ACCEPTED,
            kb.AUDIT_DISPOSITION_CONTINUATION_COMMITTED,
        )
        # The finalizer consumes the same resolved envelope identity.
        resolved = kb._resolved_canonical_audit_outcome(conn, fx["author"])
        assert resolved is not None
        assert resolved["evidence_sha256"] == envelope["evidence_sha256"]
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) == fx["author_run"]


def test_green_envelope_evidence_digest_is_stable(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn)
        env1 = kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
        )
        expected = kb._canonical_audit_outcome_evidence_sha256(
            env1["evidence"]
        )
        assert env1["evidence_sha256"] == expected
        # Byte-identical evidence -> byte-identical digest.
        env2 = kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
        )
        assert env2["evidence_sha256"] == env1["evidence_sha256"]


# ---------------------------------------------------------------------------
# Disposition: exactly one of FINAL_ACCEPTED / CONTINUATION_COMMITTED.
# ---------------------------------------------------------------------------

def test_disposition_final_accepted_without_continuation(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn)
        envelope = kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
        )
        fact = json.loads(_events(conn, fx["audit"], kb.CHANGED_FACT_EVENT)[0]["payload"])
        assert fact["disposition"] == kb.AUDIT_DISPOSITION_FINAL_ACCEPTED
        assert fact["continuation_task_ids"] == []


def test_disposition_continuation_committed_with_target(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn, continuation_child=True)
        merge = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id=? AND child_id!=?",
            (fx["author"], fx["audit"]),
        ).fetchone()["child_id"]
        envelope = kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
        )
        fact = json.loads(_events(conn, fx["audit"], kb.CHANGED_FACT_EVENT)[0]["payload"])
        assert fact["disposition"] == kb.AUDIT_DISPOSITION_CONTINUATION_COMMITTED
        assert fact["continuation_task_ids"] == [merge]


def test_disposition_skips_terminal_children(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn, continuation_child=True)
        merge = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id=? AND child_id!=?",
            (fx["author"], fx["audit"]),
        ).fetchone()["child_id"]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (merge,))
        conn.commit()
        envelope = kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
        )
        fact = json.loads(_events(conn, fx["audit"], kb.CHANGED_FACT_EVENT)[0]["payload"])
        assert fact["disposition"] == kb.AUDIT_DISPOSITION_FINAL_ACCEPTED
        assert fact["continuation_task_ids"] == []


# ---------------------------------------------------------------------------
# Hostile identity / evidence / role matrix fails closed with zero mutation.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__("head", "not-a-sha"),
        lambda m: m.__setitem__("tree", "zzz"),
        lambda m: m.__setitem__("base", "12345"),
        lambda m: m.__setitem__("repository", "attacker/repo"),
        lambda m: m.__setitem__("pr", 1),
        lambda m: m.__setitem__("github_review_id", 1),
        lambda m: m.__setitem__("github_review_url", "https://example.com/x"),
        lambda m: m.__setitem__("github_review_state", "CHANGES_REQUESTED"),
        lambda m: m.pop("head"),
        lambda m: m.pop("github_review_id"),
    ],
)
def test_hostile_malformed_evidence_fails_closed(kanban_home, mutate):
    """Malformed/foreign completion evidence fails closed (raises, zero write):
    the author's metadata must normalize to the authenticated verdict identity."""
    with kb.connect() as conn:
        fx = _fixture(conn)
        metadata = _evidence_metadata()
        mutate(metadata)
        before = _snapshot(conn)
        with pytest.raises(kb._CanonicalAuditOutcomeError):
            kb._bind_canonical_audit_outcome(
                conn, fx["audit"], fx["audit_run"], metadata,
            )
        assert _snapshot(conn) == before
        assert _events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT) == []


def _coherent_wrong_evidence():
    """A coherent, valid-format but WRONG evidence identity (different valid
    40-hex SHAs / PR / review id with a matching canonical URL)."""
    wrong_pr = 999
    wrong_review_id = 888888
    return {
        "review_outcome": "approved",
        "repository": REPOSITORY,
        "pr": wrong_pr,
        "head": "1" * 40,
        "tree": "2" * 40,
        "base": "3" * 40,
        "github_review_id": wrong_review_id,
        "github_review_url": (
            f"https://github.com/{REPOSITORY}/pull/{wrong_pr}"
            f"#pullrequestreview-{wrong_review_id}"
        ),
        "github_review_state": "APPROVED",
    }


def test_hostile_coherent_wrong_evidence_fails_closed(kanban_home):
    """The exact defect the auditor flagged: a coherent valid-format but wrong
    head/tree/base + PR/URL + review-id/URL must be rejected, not persisted as a
    resolver-accepted envelope / finalizer authority."""
    with kb.connect() as conn:
        fx = _fixture(conn)
        before = _snapshot(conn)
        with pytest.raises(kb._CanonicalAuditOutcomeError):
            kb._bind_canonical_audit_outcome(
                conn, fx["audit"], fx["audit_run"], _coherent_wrong_evidence(),
            )
        assert _snapshot(conn) == before
        assert _events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT) == []
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_no_metadata_evidence_adopts_verdict_identity(kanban_home):
    """The author supplies no evidence: the authenticated verdict identity is
    adopted as the envelope evidence (metadata is not the source of truth)."""
    with kb.connect() as conn:
        fx = _fixture(conn)
        envelope = kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"], None,
        )
        assert envelope is not None
        assert envelope["evidence"] == _evidence_block()
        assert envelope["evidence"]["head"] == HEAD


def test_v1_pass_verdict_produces_no_envelope(kanban_home):
    """A version-1 PASS verdict carries no structured evidence, so the new
    producer cannot authenticate an envelope (returns None, zero write)."""
    with kb.connect() as conn:
        author = kb.create_task(
            conn, title="implementation", factory_build_gate=1,
            assignee=AUTHOR_PROFILE,
        )
        author_run = _claim(conn, author)
        audit = kb.create_task(
            conn, title="exact-head audit", assignee=AUDITOR_PROFILE,
            parents=[author],
        )
        assert kb.request_review_handoff(
            conn, author, expected_run_id=author_run, review_task_id=audit,
            reason=f"PR #{PR} frozen at exact head {HEAD}",
        ) is not None
        audit_run = _claim(conn, audit)
        # No evidence -> version-1 verdict.
        assert kb.record_review_verdict(
            conn, author, review_task_id=audit,
            expected_review_run_id=audit_run, verdict="pass",
            reason=PRECURSOR_REASON,
        )
        before = _snapshot(conn)
        assert kb._bind_canonical_audit_outcome(
            conn, audit, audit_run, _evidence_metadata(),
        ) is None
        assert _snapshot(conn) == before
        assert _events(conn, audit, kb.CANONICAL_AUDIT_OUTCOME_EVENT) == []


def test_ambiguous_parent_fails_closed(kanban_home):
    """With valid evidence, an ambiguous author edge fails closed (zero write)."""
    with kb.connect() as conn:
        fx = _fixture(conn)
        other = kb.create_task(conn, title="other parent", assignee="merger")
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (other, fx["audit"]),
        )
        conn.commit()
        before = _snapshot(conn)
        with pytest.raises(kb._CanonicalAuditOutcomeError):
            kb._bind_canonical_audit_outcome(
                conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
            )
        assert _snapshot(conn) == before


def test_foreign_continuation_fails_closed(kanban_home):
    """A continuation target that does not resolve to a live task fails closed."""
    with kb.connect() as conn:
        fx = _fixture(conn)
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (fx["author"], "t_ghost_missing"),
        )
        conn.commit()
        before = _snapshot(conn)
        with pytest.raises(kb._CanonicalAuditOutcomeError):
            kb._bind_canonical_audit_outcome(
                conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
            )
        assert _snapshot(conn) == before


def test_ordinary_completion_produces_no_envelope(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn)
        # A non-PASS completion (no bound PASS for the exact run) is not a
        # trigger: e.g. a request_changes-like terminalization.
        assert kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"] + 999, _evidence_metadata(),
        ) is None
        assert _events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT) == []


def test_role_separation_violation_fails_closed(kanban_home):
    with kb.connect() as conn:
        # A same-profile author/auditor cannot form a handoff, so the envelope
        # never triggers; this is the upstream guard, exercised end-to-end.
        author = kb.create_task(
            conn, title="a", factory_build_gate=1, assignee=AUTHOR_PROFILE,
        )
        author_run = _claim(conn, author)
        audit = kb.create_task(
            conn, title="audit", assignee=AUTHOR_PROFILE, parents=[author],
        )
        assert kb.request_review_handoff(
            conn, author, expected_run_id=author_run, review_task_id=audit,
            reason="x",
        ) is None
        # No bound PASS -> no envelope (returns None, not a failure).
        assert kb._bind_canonical_audit_outcome(
            conn, audit, author_run, _evidence_metadata(),
        ) is None


# ---------------------------------------------------------------------------
# Resolver: hostile envelope shapes are rejected.
# ---------------------------------------------------------------------------

def test_resolved_envelope_rejects_tampered_digest(kanban_home):
    with kb.connect() as conn:
        fx = _fixture(conn)
        envelope = kb._bind_canonical_audit_outcome(
            conn, fx["audit"], fx["audit_run"], _evidence_metadata(),
        )
        tampered = dict(envelope)
        tampered["evidence"] = dict(envelope["evidence"], pr=1)
        # Persist a tampered envelope directly; the resolver must reject it.
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fx["author"], fx["author_run"], kb.CANONICAL_AUDIT_OUTCOME_EVENT,
                 json.dumps(tampered), 999999999),
            )
        # Latest envelope is the tampered one and fails digest/evidence check.
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


@pytest.mark.parametrize("mutate", [
    lambda e: e.update({"audit_task_id": "t_foreign_audit"}),
    lambda e: e.update({"author_run_id": e["author_run_id"] + 999}),
    lambda e: e.update({"audit_run_id": e["audit_run_id"] + 999}),
    lambda e: e.update({"handoff_event_id": e["handoff_event_id"] + 999}),
    lambda e: e.update({"role_separation": {
        "author_profile": "evil_author", "auditor_profile": "evil_auditor",
    }}),
])
def test_resolved_envelope_rejects_mirrored_identity_drift(kanban_home, mutate):
    """A drifted mirrored identity (audit task / author run / audit run /
    handoff event / role profiles) must not resolve: the resolver re-validates
    the envelope against live DB state and fails closed."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        envelope = fx["envelope"]
        tampered = copy.deepcopy(envelope)
        mutate(tampered)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fx["author"], fx["author_run"], kb.CANONICAL_AUDIT_OUTCOME_EVENT,
                 json.dumps(tampered), 999999999),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


# ---------------------------------------------------------------------------
# Real complete_task path: the producer corroborates completion metadata
# against the authenticated verdict identity and fails closed.
# ---------------------------------------------------------------------------

def test_complete_task_matching_evidence_binds_envelope(kanban_home):
    """The happy path through the real terminal writer: completing the audit
    with matching evidence binds one canonical envelope (no drift)."""
    with kb.connect() as conn:
        fx = _fixture(conn)
        assert kb.complete_task(
            conn, fx["audit"], expected_run_id=fx["audit_run"],
            metadata=_evidence_metadata(),
        )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is not None


def test_complete_task_coherent_wrong_evidence_fails_closed(kanban_home):
    """The exact auditor finding, through the real complete_task path: a
    coherent valid-format but wrong evidence identity must fail closed with
    zero mutation (the audit task/run stay untouched)."""
    with kb.connect() as conn:
        fx = _fixture(conn)
        before = _snapshot(conn)
        with pytest.raises(kb._CanonicalAuditOutcomeError):
            kb.complete_task(
                conn, fx["audit"], expected_run_id=fx["audit_run"],
                metadata=_coherent_wrong_evidence(),
            )
        assert _snapshot(conn) == before
        assert _events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT) == []
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (fx["audit"],),
        ).fetchone()
        assert row["status"] == "running"
        assert row["current_run_id"] == fx["audit_run"]


# ---------------------------------------------------------------------------
# Resolver mirror corroboration: a lone author envelope (no audit envelope
# mirror, no changed-fact mirrors) must not grant finalizer authority.
# ---------------------------------------------------------------------------

def _bound_fixture(conn, *, continuation_child: bool = False):
    """A valid envelope produced by the real same-run terminal writer."""
    fx = _fixture(conn, continuation_child=continuation_child)
    assert kb.complete_task(
        conn,
        fx["audit"],
        expected_run_id=fx["audit_run"],
        metadata=_evidence_metadata(),
    )
    envelope = json.loads(
        _events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT)[0]["payload"]
    )
    assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is not None
    fx["envelope"] = envelope
    return fx


def _completed_fixture(conn, *, continuation_child: bool = False):
    """A fixture produced by the real verdict -> complete_task workflow."""
    return _bound_fixture(conn, continuation_child=continuation_child)


def _delete_events(conn, task_id, kind):
    with kb.write_txn(conn):
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, kind),
        )


def test_resolver_rejects_missing_audit_envelope_mirror(kanban_home):
    """A lone author envelope with no audit envelope mirror must not resolve."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        _delete_events(conn, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT)
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_missing_author_changed_fact_mirror(kanban_home):
    """A missing author changed_fact/disposition mirror must not resolve."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        _delete_events(conn, fx["author"], kb.CHANGED_FACT_EVENT)
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_missing_audit_changed_fact_mirror(kanban_home):
    """A missing audit changed_fact mirror must not resolve."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        _delete_events(conn, fx["audit"], kb.CHANGED_FACT_EVENT)
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_audit_mirror_drift(kanban_home):
    """An audit envelope mirror that drifts from the author envelope fails closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        drifted = copy.deepcopy(fx["envelope"])
        drifted["evidence"] = dict(drifted["evidence"], head="f" * 40)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fx["audit"], fx["audit_run"], kb.CANONICAL_AUDIT_OUTCOME_EVENT,
                 json.dumps(drifted), 999999999),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_changed_fact_mirror_mismatch(kanban_home):
    """Author vs audit changed_fact mirrors must be byte-identical; a drifted
    author mirror must not resolve."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn, continuation_child=True)
        fact = json.loads(_events(conn, fx["author"], kb.CHANGED_FACT_EVENT)[0]["payload"])
        tampered = dict(fact, disposition=kb.AUDIT_DISPOSITION_FINAL_ACCEPTED)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fx["author"], fx["author_run"], kb.CHANGED_FACT_EVENT,
                 json.dumps(tampered), 999999999),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


@pytest.mark.parametrize("mutate", [
    # FINAL_ACCEPTED with a non-empty continuation (xor violation).
    lambda f: f.update(
        {"disposition": kb.AUDIT_DISPOSITION_FINAL_ACCEPTED,
         "continuation_task_ids": ["t_orphan"]},
    ),
    # CONTINUATION_COMMITTED with no targets.
    lambda f: f.update(
        {"disposition": kb.AUDIT_DISPOSITION_CONTINUATION_COMMITTED,
         "continuation_task_ids": []},
    ),
    # A foreign continuation target that is not an author child link.
    lambda f: f.update(
        {"disposition": kb.AUDIT_DISPOSITION_CONTINUATION_COMMITTED,
         "continuation_task_ids": ["t_ghost_foreign"]},
    ),
    # Duplicate continuation target ids.
    lambda f: f.update(
        {"disposition": kb.AUDIT_DISPOSITION_CONTINUATION_COMMITTED,
         "continuation_task_ids": ["t_dup", "t_dup"]},
    ),
    # Unknown disposition value.
    lambda f: f.update({"disposition": "AMBIGUOUS"}),
    # Changed fact drifts from the envelope digest.
    lambda f: f.update({"audit_outcome_sha256": "0" * 64}),
])
def test_resolver_rejects_drifted_disposition_or_continuation(kanban_home, mutate):
    """A changed_fact whose disposition / continuation / digest drifts must not
    resolve (both mirrors tampered identically so only the drift is at fault)."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn, continuation_child=True)
        fact = json.loads(_events(conn, fx["audit"], kb.CHANGED_FACT_EVENT)[0]["payload"])
        tampered = copy.deepcopy(fact)
        mutate(tampered)
        for task_id in (fx["author"], fx["audit"]):
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task_id, fx["audit_run"], kb.CHANGED_FACT_EVENT,
                     json.dumps(tampered), 999999999),
                )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


# ---------------------------------------------------------------------------
# Fail-closed no-fallback: a malformed present v3 must NOT fall through to
# the legacy receipt (the exact second half of the auditor finding).
# ---------------------------------------------------------------------------

def _legacy_receipt_fixture(conn):
    """Author -> audit with a version-1 PASS verdict (no structured evidence),
    terminalized by the ordinary writer.  No canonical audit-outcome envelope
    is produced (a v1 verdict has no authenticated evidence), so the legacy
    ``_canonical_audit_receipt`` is the sole authority path."""
    author = kb.create_task(
        conn, title="legacy reviewed author", factory_build_gate=1,
        assignee=AUTHOR_PROFILE,
    )
    author_run = _claim(conn, author)
    audit = kb.create_task(
        conn, title="legacy exact-head audit", assignee=AUDITOR_PROFILE,
        parents=[author],
    )
    assert kb.request_review_handoff(
        conn, author, expected_run_id=author_run, review_task_id=audit,
        reason=f"PR #{PR} frozen at exact head {HEAD}",
    ) is not None
    audit_run = _claim(conn, audit)
    # Version-1 verdict: no structured evidence block.
    assert kb.record_review_verdict(
        conn, author, review_task_id=audit,
        expected_review_run_id=audit_run, verdict="pass",
        reason=PRECURSOR_REASON,
    )
    assert kb.complete_task(
        conn, audit, expected_run_id=audit_run, summary="legacy audit passed",
    )
    return {"author": author, "author_run": author_run,
            "audit": audit, "audit_run": audit_run}


def test_finalizer_fails_closed_on_malformed_present_v3(kanban_home):
    """A legacy receipt resolves on its own, but the presence of a malformed
    v3 canonical envelope must fail closed: the finalizer may not fall through
    to the legacy receipt over a v3 record it never authenticated."""
    with kb.connect() as conn:
        fx = _legacy_receipt_fixture(conn)
        assert kb._canonical_audit_outcome_event_present(conn, fx["author"]) is False
        assert kb._canonical_audit_receipt(conn, fx["author"]) is not None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) == fx["author_run"]
        # Inject a malformed v3 envelope (present but fails digest/evidence).
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fx["author"], fx["author_run"], kb.CANONICAL_AUDIT_OUTCOME_EVENT,
                 json.dumps({"version": 3, "evidence": {"pr": 1}}), 999999999),
            )
        assert kb._canonical_audit_outcome_event_present(conn, fx["author"]) is True
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        # Fail closed: no legacy fallthrough.
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


# ---------------------------------------------------------------------------
# Round-4 resolver hardening: bind event rows to exact live run/profile
# provenance and enforce exact live disposition/continuation-set equality.
# ---------------------------------------------------------------------------

def test_resolver_rejects_drifted_author_run_profile(kanban_home):
    """The author run must reference a live task_runs row bound to the
    author's live assignee; a drifted profile fails closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET profile = ? WHERE id = ?",
                ("evil_author", fx["author_run"]),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_drifted_audit_run_profile(kanban_home):
    """The audit run must reference a live task_runs row bound to the
    auditor's live assignee; a drifted profile fails closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET profile = ? WHERE id = ?",
                ("evil_auditor", fx["audit_run"]),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_missing_author_run_row(kanban_home):
    """A fabricated author run id (no live task_runs row) fails closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_runs WHERE id = ?", (fx["author_run"],))
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_missing_audit_run_row(kanban_home):
    """A fabricated audit run id (no live task_runs row) fails closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_runs WHERE id = ?", (fx["audit_run"],))
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_drifted_author_envelope_event_run(kanban_home):
    """The author envelope event must be bound to the exact author run; a
    drifted event run fails closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET run_id = ? WHERE task_id = ? AND kind = ?",
                (fx["author_run"] + 999, fx["author"], kb.CANONICAL_AUDIT_OUTCOME_EVENT),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_drifted_audit_envelope_mirror_run(kanban_home):
    """The audit envelope mirror event must be bound to the exact audit run;
    a drifted event run fails closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET run_id = ? WHERE task_id = ? AND kind = ?",
                (fx["audit_run"] + 999, fx["audit"], kb.CANONICAL_AUDIT_OUTCOME_EVENT),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_drifted_author_changed_fact_run(kanban_home):
    """The author changed_fact mirror must be bound to the exact author run."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET run_id = ? WHERE task_id = ? AND kind = ?",
                (fx["author_run"] + 999, fx["author"], kb.CHANGED_FACT_EVENT),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_drifted_audit_changed_fact_run(kanban_home):
    """The audit changed_fact mirror must be bound to the exact audit run."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET run_id = ? WHERE task_id = ? AND kind = ?",
                (fx["audit_run"] + 999, fx["audit"], kb.CHANGED_FACT_EVENT),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


def test_resolver_rejects_late_final_accepted_child(kanban_home):
    """A FINAL_ACCEPTED envelope that later gains a live child must fail
    closed (the recorded disposition no longer matches the live graph)."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn)  # FINAL_ACCEPTED (no continuation child)
        kb.create_task(
            conn, title="late merge", assignee="merger", parents=[fx["author"]],
        )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_non_exhaustive_continuation_subset(kanban_home):
    """A CONTINUATION_COMMITTED envelope whose recorded continuation is a
    non-exhaustive subset of the live children must fail closed."""
    with kb.connect() as conn:
        fx = _bound_fixture(conn, continuation_child=True)  # [merge1]
        kb.create_task(
            conn, title="second merge", assignee="merger", parents=[fx["author"]],
        )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None


# ---------------------------------------------------------------------------
# Round-5 resolver hardening: authenticate the exact terminal task/run
# projections, the paired v2 verdict family, and the verdict reason through
# the real verdict -> complete_task producer path.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", ["author", "audit"])
def test_resolver_rejects_reopened_terminal_run(kanban_home, side):
    """Reopening either terminal run revokes canonical finalizer authority."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        run_id = fx[f"{side}_run"]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status='running', outcome=NULL, ended_at=NULL "
                "WHERE id=?",
                (run_id,),
            )
        reopened = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?", (run_id,),
        ).fetchone()
        assert tuple(reopened) == ("running", None, None)
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


@pytest.mark.parametrize("side", ["author", "audit"])
def test_resolver_rejects_reopened_terminal_task(kanban_home, side):
    """Both author review and audit done task projections are authoritative."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        task_id = fx[side]
        run_id = fx[f"{side}_run"]
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
                (run_id, task_id),
            )
        reopened = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        assert tuple(reopened) == ("running", run_id)
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_missing_author_v2_verdict_mirror(kanban_home):
    """The audit-side v2 PASS cannot resolve without its author-side pair."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_events WHERE task_id=? AND kind='review_verdict' "
                "AND run_id=?",
                (fx["author"], fx["audit_run"]),
            )
        assert _events(conn, fx["author"], "review_verdict") == []
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_envelope_reason_drift_from_verdict(kanban_home):
    """Byte-identical envelope mirrors cannot rewrite the bound verdict reason."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        for task_id in (fx["author"], fx["audit"]):
            row = _events(conn, task_id, kb.CANONICAL_AUDIT_OUTCOME_EVENT)[0]
            envelope = json.loads(row["payload"])
            envelope["reason"] = "drifted reason not authenticated by the v2 verdict"
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_events SET payload=? WHERE id=?",
                    (json.dumps(envelope), row["id"]),
                )
        assert all(
            json.loads(_events(conn, task_id, kb.CANONICAL_AUDIT_OUTCOME_EVENT)[0]["payload"])[
                "reason"
            ].startswith("drifted")
            for task_id in (fx["author"], fx["audit"])
        )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


# ---------------------------------------------------------------------------
# Round-6 resolver hardening: stale review rounds, closed schemas, and exact
# canonical-family cardinality must revoke old finalizer authority.
# ---------------------------------------------------------------------------

def test_resolver_rejects_stale_outcome_after_newer_review_round(kanban_home):
    """A later real handoff/run family revokes the older v3 outcome."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (fx["author"],))
        newer_author_run = _claim(conn, fx["author"])
        newer_audit = kb.create_task(
            conn, title="newer exact-head audit", assignee=AUDITOR_PROFILE,
            parents=[fx["author"]],
        )
        assert kb.request_review_handoff(
            conn, fx["author"], expected_run_id=newer_author_run,
            review_task_id=newer_audit, reason="newer exact-head review round",
        ) is not None
        newer_audit_run = _claim(conn, newer_audit)
        assert kb.complete_task(
            conn, newer_audit, expected_run_id=newer_audit_run,
            summary="terminal sibling audit without PASS authority",
        )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


@pytest.mark.parametrize("side", ["author", "audit"])
@pytest.mark.parametrize("kind", [kb.CANONICAL_AUDIT_OUTCOME_EVENT, kb.CHANGED_FACT_EVENT])
def test_resolver_rejects_duplicate_canonical_family_row(kanban_home, kind, side):
    """Even a byte-identical duplicate makes either mirror ambiguous."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        original = _events(conn, fx[side], kind)[0]
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (fx[side], original["run_id"], kind, original["payload"], 999999999),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


@pytest.mark.parametrize("kind", [kb.CANONICAL_AUDIT_OUTCOME_EVENT, kb.CHANGED_FACT_EVENT])
def test_resolver_rejects_mirrored_unknown_canonical_field(kanban_home, kind):
    """Canonical envelope/fact schemas are closed, even when mirrors agree."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        for task_id in (fx["author"], fx["audit"]):
            event = _events(conn, task_id, kind)[0]
            payload = json.loads(event["payload"])
            payload["unknown_extension"] = "must-fail-closed"
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_events SET payload=? WHERE id=?",
                    (json.dumps(payload), event["id"]),
                )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


@pytest.mark.parametrize("kind", [kb.CANONICAL_AUDIT_OUTCOME_EVENT, kb.CHANGED_FACT_EVENT])
def test_resolver_rejects_non_byte_identical_mirror(kanban_home, kind):
    """Semantically equal but differently serialized mirrors are not identical."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        event = _events(conn, fx["audit"], kind)[0]
        payload = json.loads(event["payload"])
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(payload, indent=2), event["id"]),
            )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None


def test_resolver_rejects_mirrored_created_at_drift(kanban_home):
    """Envelope time is bound to both immutable event projections."""
    with kb.connect() as conn:
        fx = _completed_fixture(conn)
        for task_id in (fx["author"], fx["audit"]):
            event = _events(conn, task_id, kb.CANONICAL_AUDIT_OUTCOME_EVENT)[0]
            payload = json.loads(event["payload"])
            payload["created_at"] += 1
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_events SET payload=? WHERE id=?",
                    (json.dumps(payload), event["id"]),
                )
        assert kb._resolved_canonical_audit_outcome(conn, fx["author"]) is None
        assert kb._reviewed_author_finalizer_run_id(conn, fx["author"]) is None
