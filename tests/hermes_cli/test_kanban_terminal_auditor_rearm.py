"""Regression coverage for rearming one terminal REQUEST_CHANGES auditor."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


OLD_HEAD = "1" * 40
OLD_TREE = "2" * 40
OLD_BASE = "3" * 40
NEW_HEAD = "4" * 40
PR = 79
REVIEW_ID = 12345
REASON = f"PR #{PR} frozen at exact head {NEW_HEAD} for independent audit"


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


def _task(conn, task_id):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _claim_run(conn, task_id):
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    return claimed.current_run_id


def _archive(conn, task_id):
    status = _task(conn, task_id).status
    if status != "todo":
        assert kb.unblock_task(conn, task_id)
        assert _task(conn, task_id).status == "todo"
    with kb._authenticated_strict_orchestrator_archive():
        assert kb.archive_task(
            conn,
            task_id,
            reason=(
                f"superseded prior-head {OLD_HEAD} branch after run3732 and "
                f"review {REVIEW_ID}; "
                "zero action"
            ),
            actor="kanban-orchestrator",
            source="kanban_archive",
            fail_if_active_run=True,
            expected_status="todo",
        )


def _bind_kernel_factory_receipt(conn, task_id, run_id):
    receipt = {
        "schema": kb.FACTORY_RECEIPT_SCHEMA,
        "verdict": kb.FACTORY_VERDICT_ACCEPTED,
        "conditions": {key: True for key in kb.FACTORY_CONDITION_KEYS},
        "contract_hash_sha256": kb.FACTORY_CONTRACT_HASH_SHA256,
        "kernel_version": "fixture-kernel-v1",
        "adapter_type_and_version": "fixture-adapter-v1",
        "exact_source_refs": {"fixture": True},
        "target_identity": {"task_id": task_id},
        "before_digest": "1" * 64,
        "action_receipt_ref": {
            "actor_identity_source": "provider_runtime_metadata",
            "actor_role": "action_executor",
        },
        "after_digest": "2" * 64,
        "head_epoch_or_run_binding": {"run_id": run_id},
        "acquired_at": 1_788_306_000,
    }
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    kb.store_attachment_bytes(
        conn,
        task_id,
        "aion_monarch_receipt.json",
        raw,
        content_type="application/json",
        uploaded_by="aion_monarch_proof_kernel",
    )
    conn.execute(
        "UPDATE tasks SET factory_terminal_receipt_sha256=? WHERE id=?",
        (digest, task_id),
    )


def _terminal_request_changes_graph(conn, *, authenticated_capability=True):
    author = kb.create_task(
        conn,
        title="same reviewed author",
        assignee="agent007",
        workspace_kind="dir",
        workspace_path="/tmp/preserved-author-workspace",
        provider_override="openai-codex",
        model_override="gpt-5.6-sol",
    )
    auditor = kb.create_task(
        conn,
        title="same terminal auditor",
        assignee="bafuxunan",
        parents=[author],
        workspace_kind="dir",
        workspace_path="/tmp/preserved-auditor-workspace",
        provider_override="anthropic",
        model_override="claude-sonnet-4",
    )

    first_author_run = _claim_run(conn, author)
    assert kb.request_review_handoff(
        conn,
        author,
        expected_run_id=first_author_run,
        review_task_id=auditor,
        reason=f"PR #{PR} initial exact head {OLD_HEAD}",
    )
    first_audit_run = _claim_run(conn, auditor)
    assert kb.record_review_verdict(
        conn,
        author,
        review_task_id=auditor,
        expected_review_run_id=first_audit_run,
        verdict="request_changes",
        reason="REQUEST_CHANGES_INITIAL_ROUND",
    )

    second_author_run = _claim_run(conn, author)
    assert kb.request_review_handoff(
        conn,
        author,
        expected_run_id=second_author_run,
        review_task_id=auditor,
        reason=f"PR #{PR} repaired at exact head {OLD_HEAD}",
    )
    second_audit_run = _claim_run(conn, auditor)
    assert kb.record_review_verdict(
        conn,
        author,
        review_task_id=auditor,
        expected_review_run_id=second_audit_run,
        verdict="pass",
        reason="PASS_WITH_RUNTIME_LIMITS",
    )
    recovery_reason = "REQUEST_CHANGES_EXACT_HEAD: terminal corrected prior head"
    receipt = {
        "review_outcome": "REQUEST_CHANGES_EXACT_HEAD",
        "repository": "kiddhu/aion-governance",
        "pr": PR,
        "head": OLD_HEAD,
        "tree": OLD_TREE,
        "base": OLD_BASE,
        "github_review_id": REVIEW_ID,
        "github_review_url": (
            f"https://github.com/kiddhu/aion-governance/pull/{PR}"
            f"#pullrequestreview-{REVIEW_ID}"
        ),
        "github_review_state": "CHANGES_REQUESTED",
    }
    assert kb.complete_task(
        conn,
        auditor,
        expected_run_id=second_audit_run,
        summary=recovery_reason,
        metadata={
            "corrected_final_verdict": "REQUEST_CHANGES_EXACT_HEAD",
            "repository": receipt["repository"],
            "pr": receipt["pr"],
            "head": receipt["head"],
            "tree": receipt["tree"],
            "base": receipt["base"],
            "github_review_id": receipt["github_review_id"],
            "github_review_url": receipt["github_review_url"],
            "merge_allowed": False,
            "true_done": False,
            "forbidden_actions_performed": [],
        },
    )

    fact = kb.create_task(
        conn, title="prior-head read-only fact", assignee="bafuxunan", parents=[auditor]
    )
    fact_run = _claim_run(conn, fact)
    assert kb.complete_task(
        conn,
        fact,
        expected_run_id=fact_run,
        summary="prior-head facts only",
        metadata={
            "external_mutations_performed": [],
            "repository": receipt["repository"],
            "pr": receipt["pr"],
            "head": receipt["head"],
            "review_id": receipt["github_review_id"],
            "review_state": receipt["github_review_state"],
        },
    )

    archived = kb.create_task(
        conn,
        title="superseded merger",
        body=f"prior audited head {OLD_HEAD}",
        assignee="merger",
        parents=[auditor],
    )
    archived_run = _claim_run(conn, archived)
    assert kb.block_task(
        conn,
        archived,
        reason="fail closed before merge; zero action",
        kind="needs_input",
        expected_run_id=archived_run,
    )
    kb.link_tasks(conn, author, archived)
    nested = kb.create_task(
        conn, title="superseded activation", assignee="agent007", parents=[archived]
    )
    _archive(conn, nested)
    _archive(conn, archived)

    controller = kb.create_task(
        conn, title="prior-head correction controller", assignee="gm2", parents=[auditor]
    )
    controller_run = _claim_run(conn, controller)
    prior_rows = conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id=? "
        "AND kind='review_verdict' ORDER BY id",
        (author,),
    ).fetchall()
    audit_runs = conn.execute(
        "SELECT id, profile, status, outcome, summary, metadata, ended_at "
        "FROM task_runs WHERE task_id=? ORDER BY id DESC",
        (auditor,),
    ).fetchall()
    assert kb._authenticated_same_auditor_terminal_correction_source_run_id(
        conn, author, auditor, second_audit_run, prior_rows, audit_runs
    ) == second_author_run
    metadata = json.loads(audit_runs[0]["metadata"])
    assert kb._legacy_terminal_correction_recovery_receipt(metadata, receipt) == receipt
    assert _task(conn, author).status == "review"
    assert conn.execute(
        "SELECT status, assignee, current_run_id FROM tasks WHERE id=?", (controller,)
    ).fetchone()["current_run_id"] == controller_run
    assert kb.record_review_verdict(
        conn,
        author,
        review_task_id=auditor,
        expected_review_run_id=second_audit_run,
        verdict="request_changes",
        reason=recovery_reason,
        recovery_receipt=receipt,
        controller_task_id=controller,
        controller_run_id=controller_run,
        controller_profile="gm2",
    )
    assert kb.complete_task(
        conn,
        controller,
        expected_run_id=controller_run,
        summary="same author recovered from prior-head correction",
        metadata={
            "boundaries": {
                "forbidden_actions_performed": [],
                "raw_db_write": False,
                "external_side_effect": False,
            },
            "bound_head": OLD_HEAD,
            "audit_binding": {
                "review_task_id": auditor,
                "review_run_id": second_audit_run,
                "github_review_id": receipt["github_review_id"],
                "github_review_state": receipt["github_review_state"],
            },
        },
    )

    capability = kb.create_task(
        conn,
        title="factory terminal-rearm capability",
        assignee="gm2",
        factory_build_gate=1 if authenticated_capability else 0,
    )
    capability_run = _claim_run(conn, capability)
    if authenticated_capability:
        _bind_kernel_factory_receipt(conn, capability, capability_run)
    implementation = kb.create_task(
        conn,
        title="bounded terminal-rearm implementation",
        assignee="agent007",
        parents=[capability],
    )
    kb.link_tasks(conn, implementation, author)
    entries = sorted(
        [
            {"task_id": auditor, "effect_class": "TERMINAL_AUDITOR_SOURCE"},
            {
                "task_id": fact,
                "effect_class": "GM2_ATTESTED_OBSERVATION_ONLY_FACTORY_DONE",
            },
            {
                "task_id": archived,
                "effect_class": "STRICT_ARCHIVED_NO_COMPLETED_ACTION",
            },
            {
                "task_id": nested,
                "effect_class": "STRICT_ARCHIVED_NO_COMPLETED_ACTION",
            },
            {
                "task_id": controller,
                "effect_class": "TYPED_NATIVE_RECOVERY_RESOLVED_FACTORY_DONE",
            },
        ],
        key=lambda entry: entry["task_id"],
    )
    manifest_sha256 = kb._terminal_rearm_manifest(
        conn, root_task_id=auditor, entries=entries,
    )
    assert manifest_sha256 is not None
    recovery_event = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id=? "
        "AND kind='review_verdict' AND payload LIKE '%\"version\": 2%'",
        (author,),
    ).fetchone()
    assert recovery_event is not None
    capability_metadata = {
        "schema": kb._TERMINAL_REARM_CAPABILITY_SCHEMA,
        "version": 1,
        "decision": "AUTHORIZED_SAME_TASK_CAPABILITY_PATH",
        "capability_id": "a" * 64,
        "authority": {
            "profile": "gm2",
            "run_id": capability_run,
            "task_id": capability,
            "terminal_auth": "KERNEL_FACTORY_TERMINAL_RECEIPT_REQUIRED",
        },
        "authorized_handoff": {
            "author_task_id": author,
            "auditor_task_id": auditor,
            "authorized_current_head": NEW_HEAD,
            "authorized_current_parent": "5" * 40,
            "authorized_current_tree": "6" * 40,
            "max_logical_uses": 1,
            "observed_pr_base_non_authoritative": "7" * 40,
            "pr": PR,
            "prior_audit_run_id": second_audit_run,
            "prior_base": OLD_BASE,
            "prior_head": OLD_HEAD,
            "prior_review_id": REVIEW_ID,
            "prior_review_state": "CHANGES_REQUESTED",
            "prior_tree": OLD_TREE,
            "repository": receipt["repository"],
            "source_blocked_run_id": second_author_run,
        },
        "consumer_contract": {
            "authority_lookup": "EXACT_ONE_AUTHENTICATED_DONE_GM2_ANCESTOR",
            "caller_payload_semantics": "OPAQUE_HASH_BOUND_NOT_AUTHORITY",
            "drift": "FAIL_CLOSED_ZERO_MUTATION",
            "metadata_contract": "EXACT_KEYS_TYPES_ENUMS_NO_ALIASES",
            "non_authoritative_sources": ["reason_text", "task_body"],
            "replay": "SAME_AUTHOR_RUN_RETURNS_ORIGINAL_RECEIPT_OTHERWISE_DENY",
            "required_ancestor_edges": [
                [capability, implementation], [implementation, author],
            ],
            "success_write": (
                "ONE_ATOMIC_EXISTING_HANDOFF_PLUS_ONE_TYPED_CONSUMPTION_EVENT"
            ),
        },
        "implementation": {
            "assignee": "agent007",
            "candidate_budget": 1,
            "failed_candidate": {
                "audit_run_id": second_audit_run,
                "audit_task_id": auditor,
                "base": OLD_BASE,
                "github_review_id": REVIEW_ID,
                "github_review_state": "CHANGES_REQUESTED",
                "head": OLD_HEAD,
                "tree": OLD_TREE,
            },
            "new_control_plane_budget": 0,
            "new_task_budget": 0,
            "pr": 79,
            "repository": "kiddhu/hermes-agent",
            "required_changed_paths": [
                "hermes_cli/kanban_db.py",
                "tests/hermes_cli/test_kanban_terminal_auditor_rearm.py",
            ],
            "task_id": implementation,
        },
        "legacy_graph": {
            "manifest_schema": kb._TERMINAL_REARM_MANIFEST_SCHEMA,
            "manifest_algorithm": kb._TERMINAL_REARM_MANIFEST_ALGORITHM,
            "manifest_sha256": manifest_sha256,
            "root_auditor_task_id": auditor,
            "task_count": len(entries),
            "entries": entries,
            "recovery_event": {
                "id": recovery_event["id"],
                "kind": recovery_event["kind"],
                "payload_sha256": hashlib.sha256(
                    recovery_event["payload"].encode("utf-8")
                ).hexdigest(),
                "version": 2,
            },
        },
        "forbidden_actions": ["REPLACEMENT_TASK_AUDITOR_OR_PR"],
        "stage_gates": ["FRESH_EXACT_HEAD_INDEPENDENT_AUDIT"],
        "stop_conditions": ["ANY_CAPABILITY_OR_GRAPH_DRIFT"],
        "worker_session_id": "fixture",
    }
    assert kb.complete_task(
        conn,
        capability,
        expected_run_id=capability_run,
        summary="authorized one closed-world capability",
        metadata=capability_metadata,
    )
    implementation_run = _claim_run(conn, implementation)
    assert kb.complete_task(
        conn,
        implementation,
        expected_run_id=implementation_run,
        summary="capability implementation dependency satisfied",
        metadata={"forbidden_actions_performed": []},
    )
    fresh_author_run = _claim_run(conn, author)
    return {
        "author": author,
        "auditor": auditor,
        "first_author_run": first_author_run,
        "first_audit_run": first_audit_run,
        "second_author_run": second_author_run,
        "second_audit_run": second_audit_run,
        "fresh_author_run": fresh_author_run,
        "fact": fact,
        "fact_run": fact_run,
        "archived": archived,
        "archived_run": archived_run,
        "nested": nested,
        "controller": controller,
        "controller_run": controller_run,
        "capability": capability,
        "capability_run": capability_run,
        "implementation": implementation,
        "receipt": receipt,
    }


def _historical_snapshot(conn, graph):
    return {
        "tasks": [
            tuple(row)
            for row in conn.execute(
                "SELECT id, assignee, workspace_kind, workspace_path, provider_override, "
                "model_override FROM tasks ORDER BY id"
            )
        ],
        "runs": [tuple(row) for row in conn.execute("SELECT * FROM task_runs ORDER BY id")],
        "links": [tuple(row) for row in conn.execute("SELECT * FROM task_links ORDER BY 1, 2")],
        "old_events": [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id != ? ORDER BY id",
                (graph["author"],),
            )
        ],
        "counts": tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_runs", "task_links")
        ),
    }


def test_rearms_same_terminal_request_changes_auditor_once(kanban_home):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(conn)
        before = _historical_snapshot(conn, graph)
        auditor_before = conn.execute(
            "SELECT id, assignee, workspace_kind, workspace_path, provider_override, "
            "model_override FROM tasks WHERE id=?",
            (graph["auditor"],),
        ).fetchone()
        handoff_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_handoff'",
            (graph["author"],),
        ).fetchone()[0]
        receipt = kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=graph["fresh_author_run"],
            review_task_id=graph["auditor"],
            reason=REASON,
        )

        assert receipt is not None
        assert receipt.review_task_id == graph["auditor"]
        auditor = _task(conn, graph["auditor"])
        assert auditor.status == "ready"
        assert auditor.current_run_id is None
        assert auditor.assignee == "bafuxunan"
        assert conn.execute(
            "SELECT id, assignee, workspace_kind, workspace_path, provider_override, "
            "model_override FROM tasks WHERE id=?",
            (graph["auditor"],),
        ).fetchone() == auditor_before
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_handoff'",
            (graph["author"],),
        ).fetchone()[0] == handoff_count + 1
        after = _historical_snapshot(conn, graph)
        assert [
            row for row in after["runs"] if row[0] != graph["fresh_author_run"]
        ] == [
            row for row in before["runs"] if row[0] != graph["fresh_author_run"]
        ]
        fresh_after = next(
            row for row in after["runs"] if row[0] == graph["fresh_author_run"]
        )
        assert fresh_after[4] == "review_required"
        assert after["links"] == before["links"]
        assert after["old_events"][:-2] == before["old_events"]
        consumed, promoted = after["old_events"][-2:]
        assert consumed[1] == graph["capability"]
        assert consumed[2] == graph["fresh_author_run"]
        assert consumed[3] == "terminal_auditor_rearm_capability_consumed"
        consumed_payload = json.loads(consumed[4])
        assert consumed_payload["author_task_id"] == graph["author"]
        assert consumed_payload["auditor_task_id"] == graph["auditor"]
        assert promoted[1] == graph["auditor"]
        assert promoted[3] == "promoted"
        assert json.loads(promoted[4]) == {
            "source": "review_handoff", "parent_id": graph["author"]
        }
        assert after["counts"] == before["counts"]
        assert after["tasks"] == before["tasks"]

        claimed = kb.claim_task(conn, graph["auditor"])
        assert claimed is not None
        assert claimed.id == graph["auditor"]
        assert claimed.current_run_id not in {
            graph["first_audit_run"], graph["second_audit_run"]
        }


@pytest.mark.parametrize(
    "invalidity",
    [
        "missing_recovery",
        "prior_pass_only",
        "nonterminal_descendant",
        "side_effectful_descendant",
        "contradictory_external_side_effect",
        "contradictory_merge_performed",
        "case_variant_merge_performed",
        "contradictory_compound_action",
        "contradictory_action_count",
        "missing_prior_round_binding",
        "recursive_done_missing_prior_round_binding",
        "recursive_done_body_prose_parent_id",
        "active_descendant_claim",
        "open_descendant_run",
        "stale_author_run",
        "missing_edge",
        "role_collision",
        "duplicate_auditor",
        "cross_repository_receipt",
        "recovery_receipt_head_mismatch",
        "ambiguous_archive",
        "archived_open_run",
        "descendant_missing_completion_event",
        "current_head_descendant",
        "recursive_archive_current_head",
        "active_auditor_claim",
        "controller_nonterminal",
        "wrong_auditor_run_profile",
    ],
)
def test_terminal_auditor_rearm_hostile_cases_are_zero_mutation(
    kanban_home, invalidity,
):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(conn)
        reason = REASON
        recovery = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? AND kind='review_verdict' "
            "AND payload LIKE '%\"version\": 2%' ORDER BY id DESC LIMIT 1",
            (graph["author"],),
        ).fetchone()
        assert recovery is not None

        if invalidity in {"missing_recovery", "prior_pass_only"}:
            conn.execute("DELETE FROM task_events WHERE id=?", (recovery["id"],))
        elif invalidity == "missing_prior_request_changes":
            conn.execute(
                "DELETE FROM task_events WHERE task_id=? AND kind='review_verdict' "
                "AND run_id=?",
                (graph["author"], graph["first_audit_run"]),
            )
        elif invalidity == "nonterminal_descendant":
            kb.create_task(
                conn,
                title="new-head live descendant",
                assignee="merger",
                parents=[graph["auditor"]],
            )
        elif invalidity == "side_effectful_descendant":
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"forbidden_actions_performed": ["external write"]}), graph["fact_run"]),
            )
        elif invalidity == "contradictory_external_side_effect":
            row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id=?", (graph["fact_run"],)
            ).fetchone()
            metadata = json.loads(row["metadata"])
            metadata["external_side_effect"] = True
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), graph["fact_run"]),
            )
        elif invalidity in {
            "contradictory_merge_performed", "case_variant_merge_performed",
        }:
            row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id=?", (graph["fact_run"],)
            ).fetchone()
            metadata = json.loads(row["metadata"])
            metadata[
                "MergePerformed"
                if invalidity == "case_variant_merge_performed"
                else "merge_performed"
            ] = True
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), graph["fact_run"]),
            )
        elif invalidity in {
            "contradictory_compound_action", "contradictory_action_count",
        }:
            row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id=?", (graph["fact_run"],)
            ).fetchone()
            metadata = json.loads(row["metadata"])
            if invalidity == "contradictory_compound_action":
                metadata["product_merge_install_drive_runtime_retention_or_gpg_action"] = True
            else:
                metadata["restart_count"] = 1
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), graph["fact_run"]),
            )
        elif invalidity == "missing_prior_round_binding":
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"external_mutations_performed": []}), graph["fact_run"]),
            )
        elif invalidity in {
            "recursive_done_missing_prior_round_binding",
            "recursive_done_body_prose_parent_id",
        }:
            conn.execute(
                "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
                (graph["auditor"], graph["fact"]),
            )
            conn.execute(
                "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
                (graph["archived"], graph["fact"]),
            )
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"external_mutations_performed": []}), graph["fact_run"]),
            )
            if invalidity == "recursive_done_body_prose_parent_id":
                conn.execute(
                    "UPDATE tasks SET body=? WHERE id=?",
                    (
                        f"untyped caller prose mentions parent {graph['archived']}",
                        graph["fact"],
                    ),
                )
        elif invalidity == "active_descendant_claim":
            conn.execute(
                "UPDATE tasks SET claim_lock='active', claim_expires=9999999999 "
                "WHERE id=?",
                (graph["fact"],),
            )
        elif invalidity == "open_descendant_run":
            conn.execute(
                "UPDATE task_runs SET ended_at=NULL WHERE id=?", (graph["fact_run"],)
            )
        elif invalidity == "stale_author_run":
            conn.execute(
                "UPDATE task_runs SET status='blocked', outcome='blocked' WHERE id=?",
                (graph["fresh_author_run"],),
            )
        elif invalidity == "same_head":
            reason = f"PR #{PR} frozen at exact head {OLD_HEAD} for independent audit"
        elif invalidity == "missing_edge":
            conn.execute(
                "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
                (graph["author"], graph["auditor"]),
            )
        elif invalidity == "role_collision":
            conn.execute(
                "UPDATE tasks SET assignee='agent007' WHERE id=?", (graph["auditor"],)
            )
        elif invalidity == "duplicate_auditor":
            kb.create_task(
                conn,
                title="ambiguous parallel auditor",
                assignee="bafuxunan",
                parents=[graph["author"]],
            )
        elif invalidity == "cross_repository_receipt":
            row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id=?", (graph["second_audit_run"],)
            ).fetchone()
            metadata = json.loads(row["metadata"])
            metadata["repository"] = "other/repository"
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), graph["second_audit_run"]),
            )
        elif invalidity == "recovery_receipt_head_mismatch":
            row = conn.execute(
                "SELECT payload FROM task_events WHERE id=?", (recovery["id"],)
            ).fetchone()
            payload = json.loads(row["payload"])
            payload["recovery_receipt"]["head"] = "f" * 40
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(payload), recovery["id"]),
            )
        elif invalidity == "ambiguous_archive":
            conn.execute(
                "DELETE FROM task_events WHERE task_id=? "
                "AND kind='strict_orchestrator_archive_authenticated'",
                (graph["archived"],),
            )
        elif invalidity == "archived_open_run":
            conn.execute(
                "UPDATE task_runs SET ended_at=NULL WHERE id=?",
                (graph["archived_run"],),
            )
        elif invalidity == "descendant_missing_completion_event":
            conn.execute(
                "DELETE FROM task_events WHERE task_id=? AND kind='completed'",
                (graph["fact"],),
            )
        elif invalidity == "current_head_descendant":
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (
                    json.dumps({
                        "external_mutations_performed": [],
                        "bound_head": NEW_HEAD,
                    }),
                    graph["fact_run"],
                ),
            )
        elif invalidity == "recursive_archive_current_head":
            archive = conn.execute(
                "SELECT id, payload FROM task_events WHERE task_id=? AND kind='archived'",
                (graph["nested"],),
            ).fetchone()
            payload = json.loads(archive["payload"])
            payload["reason"] += f"; incorrectly bound to current head {NEW_HEAD}"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?",
                (json.dumps(payload), archive["id"]),
            )
        elif invalidity == "controller_nonterminal":
            conn.execute(
                "UPDATE tasks SET status='blocked' WHERE id=?",
                (graph["controller"],),
            )
        elif invalidity == "wrong_auditor_run_profile":
            conn.execute(
                "UPDATE task_runs SET profile='elder-senate' WHERE id=?",
                (graph["second_audit_run"],),
            )
        else:
            conn.execute(
                "UPDATE tasks SET claim_lock='stale-audit-claim' WHERE id=?",
                (graph["auditor"],),
            )
        conn.commit()

        before = "\n".join(conn.iterdump())
        assert kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=graph["fresh_author_run"],
            review_task_id=graph["auditor"],
            reason=reason,
        ) is None
        assert "\n".join(conn.iterdump()) == before


def test_manifest_rejects_native_lineage_drift(kanban_home):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(conn)
        conn.execute(
            "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
            (graph["auditor"], graph["fact"]),
        )
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (graph["archived"], graph["fact"]),
        )
        created = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id=? AND kind='created'",
            (graph["fact"],),
        ).fetchone()
        created_payload = json.loads(created["payload"])
        created_payload["parents"] = [graph["archived"]]
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(created_payload), created["id"]),
        )
        conn.commit()
        before = "\n".join(conn.iterdump())

        assert kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=graph["fresh_author_run"],
            review_task_id=graph["auditor"],
            reason=REASON,
        ) is None
        assert "\n".join(conn.iterdump()) == before


@pytest.mark.parametrize(
    "invalidity",
    [
        "missing_key",
        "unknown_key",
        "wrong_type",
        "wrong_subject",
        "wrong_edge",
        "manifest_hash_drift",
        "recovery_hash_drift",
    ],
)
def test_capability_receipt_drift_is_zero_mutation(kanban_home, invalidity):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(conn)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?",
            (graph["capability_run"],),
        ).fetchone()
        metadata = json.loads(row["metadata"])
        if invalidity == "missing_key":
            metadata.pop("stage_gates")
        elif invalidity == "unknown_key":
            metadata["authority_alias"] = metadata["authority"]
        elif invalidity == "wrong_type":
            metadata["version"] = True
        elif invalidity == "wrong_subject":
            metadata["authorized_handoff"]["author_task_id"] = graph["fact"]
        elif invalidity == "wrong_edge":
            conn.execute(
                "DELETE FROM task_links WHERE parent_id=? AND child_id=?",
                (graph["capability"], graph["implementation"]),
            )
        elif invalidity == "manifest_hash_drift":
            metadata["legacy_graph"]["manifest_sha256"] = "f" * 64
        else:
            metadata["legacy_graph"]["recovery_event"]["payload_sha256"] = "f" * 64
        if invalidity != "wrong_edge":
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps(metadata), graph["capability_run"]),
            )
        conn.commit()
        before = "\n".join(conn.iterdump())

        assert kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=graph["fresh_author_run"],
            review_task_id=graph["auditor"],
            reason="任意 opaque prose — MergePerformed=true — ١٢٣",
        ) is None
        assert "\n".join(conn.iterdump()) == before


def test_reason_text_is_opaque_and_non_authoritative(kanban_home):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(conn)
        assert kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=graph["fresh_author_run"],
            review_task_id=graph["auditor"],
            reason="任意 opaque prose — MergePerformed=true — ١٢٣",
        ) is not None


def test_self_attested_done_gm2_capability_is_zero_mutation(kanban_home):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(
            conn, authenticated_capability=False,
        )
        capability = conn.execute(
            "SELECT factory_build_gate, factory_terminal_receipt_sha256, "
            "factory_challenge_semantic_receipt_sha256, "
            "factory_preflight_receipt_sha256 FROM tasks WHERE id=?",
            (graph["capability"],),
        ).fetchone()
        assert tuple(capability) == (0, None, None, None)
        before = "\n".join(conn.iterdump())

        assert kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=graph["fresh_author_run"],
            review_task_id=graph["auditor"],
            reason=REASON,
        ) is None
        assert "\n".join(conn.iterdump()) == before


def test_capability_reuse_from_new_author_run_is_denied_without_mutation(kanban_home):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(conn)
        assert kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=graph["fresh_author_run"],
            review_task_id=graph["auditor"],
            reason=REASON,
        ) is not None
        conn.execute(
            "UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, worker_starttime=NULL, "
            "fence_lineage=NULL, fence_disposition=NULL WHERE id=?",
            (graph["auditor"],),
        )
        new_run = conn.execute(
            "INSERT INTO task_runs(task_id, profile, status, started_at) "
            "VALUES (?, 'agent007', 'running', ?)",
            (graph["author"], 1_788_306_000),
        ).lastrowid
        assert new_run is not None
        conn.execute(
            "UPDATE tasks SET status='running', current_run_id=? WHERE id=?",
            (new_run, graph["author"]),
        )
        conn.commit()
        before = "\n".join(conn.iterdump())

        assert kb.request_review_handoff(
            conn,
            graph["author"],
            expected_run_id=new_run,
            review_task_id=graph["auditor"],
            reason="different opaque reason",
        ) is None
        assert "\n".join(conn.iterdump()) == before


def test_terminal_auditor_rearm_concurrent_replay_is_single_logical_handoff(
    kanban_home,
):
    with kb.connect() as conn:
        graph = _terminal_request_changes_graph(conn)
        counts_before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_runs", "task_links")
        )
        handoffs_before = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_handoff'",
            (graph["author"],),
        ).fetchone()[0]

    barrier = threading.Barrier(2)

    def handoff():
        with kb.connect() as thread_conn:
            barrier.wait()
            return kb.request_review_handoff(
                thread_conn,
                graph["author"],
                expected_run_id=graph["fresh_author_run"],
                review_task_id=graph["auditor"],
                reason=REASON,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: handoff(), range(2)))
    assert all(receipt is not None for receipt in receipts)
    assert receipts[0] == receipts[1]

    with kb.connect() as conn:
        assert _task(conn, graph["auditor"]).status == "ready"
        assert tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_runs", "task_links")
        ) == counts_before
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_handoff'",
            (graph["author"],),
        ).fetchone()[0] == handoffs_before + 1
