"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _add_prior_run(conn, tid, outcome="done", summary="prior execution"):
    """Insert a closed run so the task appears to have prior execution."""
    import time
    now = int(time.time())
    rid = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, summary, started_at, ended_at) "
        "VALUES (?, ?, 'done', ?, ?, ?, ?)",
        (tid, "worker", outcome, summary, now - 60, now - 30),
    ).lastrowid
    return rid


# ---------------------------------------------------------------------------
# Existing decompose tests
# ---------------------------------------------------------------------------


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_assigns_default_when_unassigned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just one thing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "**Goal**\nDo the thing.",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.new_title == "Tightened title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify path with no parents -> recompute_ready flips to 'ready'
    assert task.status == "ready"
    assert task.title == "Tightened title"
    assert task.assignee == "fallback"


def test_decompose_fanout_false_preserves_existing_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already routed",
            assignee="engineer",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Keep existing lane.",
        "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"
    assert task.title == "Tightened title"


def test_decompose_fanout_false_uses_valid_llm_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to specialist.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_unknown_assignee_falls_back_to_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    # Roster only has 'orchestrator' and 'fallback'; LLM picks 'made_up'.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test",
        "tasks": [
            {"title": "do X", "body": "", "assignee": "made_up", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch.dict(
            "os.environ", {}, clear=False,
        ), _patch_aux_client(llm_payload), _patch_extra_body(), \
            patch(
                "hermes_cli.kanban_decompose._load_config",
                return_value={
                    "kanban": {
                        "orchestrator_profile": "orchestrator",
                        "default_assignee": "fallback",
                    }
                },
            ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    # 'made_up' wasn't in roster, so assignee rewritten to 'fallback'
    assert child.assignee == "fallback"


def test_decompose_handles_malformed_llm_json(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not json at all, sorry"), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "malformed JSON" in outcome.reason


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_no_aux_client_configured(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        # call_llm raises RuntimeError when no provider is configured; the
        # decomposer must convert that into a failed outcome, not a crash.
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("No LLM provider configured"),
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    # call_llm's no-provider RuntimeError surfaces via the LLM-error branch.
    assert "LLM error" in outcome.reason


# ---------------------------------------------------------------------------
# RED tests — block-loop triage → auto-decompose collision
# ---------------------------------------------------------------------------


def test_decompose_skips_block_loop_triage_with_prior_runs(kanban_home):
    """RED: A task that landed in triage via block_loop_detected AND has
    prior execution runs must NOT be decomposed — the decomposer must
    skip it and return ok=False with a block-loop eligibility reason.

    This reproduces the exact t_0df failure: a repeated review-required
    block on an already-implemented task was routed to triage, and the
    auto-decomposer reassigned the canonical task and created reverse-
    parent children."""
    with kb.connect() as conn:
        # Simulate a task that was already executed (has prior runs)
        tid = kb.create_task(conn, title="fix auth bug", assignee="agent007", triage=True)
        _add_prior_run(conn, tid, outcome="done", summary="implemented the fix")

        # Simulate block_loop_detected: the task was blocked with
        # review-required, unblocked, and re-blocked for the same reason,
        # triggering the loop breaker → triage
        kb._append_event(conn, tid, "block_loop_detected", {
            "reason": "review-required: PR #862 ready for review",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        })

    patches = _patch_list_profiles(["orchestrator", "agent007", "gmaion"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    # RED: the decomposer must reject this task
    assert outcome.ok is False, f"expected skip, got ok=True: {outcome.reason}"
    assert "block-loop" in outcome.reason.lower() or "prior execution" in outcome.reason.lower()
    assert outcome.child_ids is None
    assert outcome.fanout is False

    # Verify the task is untouched: still in triage, original assignee
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.assignee == "agent007"


def test_decompose_block_loop_no_children_created(kanban_home):
    """RED: A block-loop triage task must produce ZERO child tasks."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="deploy config", assignee="agent007", triage=True)
        _add_prior_run(conn, tid, outcome="blocked",
                       summary="review-required: config change needs approval")
        kb._append_event(conn, tid, "block_loop_detected", {
            "reason": "review-required: config change needs approval",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        })

    patches = _patch_list_profiles(["orchestrator", "agent007", "gm2"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert outcome.child_ids is None

    # Hard check: no new tasks were created in the DB
    with kb.connect() as conn:
        all_tasks = kb.list_tasks(conn)
        # Only the original task should exist (no children spawned)
        assert len(all_tasks) == 1
        assert all_tasks[0].id == tid


def test_decompose_block_loop_no_reverse_parent_links(kanban_home):
    """RED: A block-loop triage task must not acquire reverse parent links
    from decomposition children."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="audit report", assignee="agent007", triage=True)
        _add_prior_run(conn, tid, outcome="done",
                       summary="audit-complete: report generated")
        kb._append_event(conn, tid, "block_loop_detected", {
            "reason": "review-required: awaiting audit sign-off",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        })

    patches = _patch_list_profiles(["orchestrator", "agent007", "auditor"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False

    # Verify: the task has no parent links (no children became parents of root)
    with kb.connect() as conn:
        links = conn.execute(
            "SELECT * FROM task_links WHERE child_id = ?", (tid,)
        ).fetchall()
        assert len(links) == 0, f"expected zero parent links, got {len(links)}"


# ---------------------------------------------------------------------------
# GREEN tests — owner preservation, idempotency, control case
# ---------------------------------------------------------------------------


def test_block_loop_skip_preserves_assignee_exactly(kanban_home):
    """GREEN: A block-loop skipped task preserves its original assignee exactly,
    with no reassignment to orchestrator or any other profile."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="security patch", assignee="agent007", triage=True)
        _add_prior_run(conn, tid, outcome="done", summary="patch applied")
        kb._append_event(conn, tid, "block_loop_detected", {
            "reason": "review-required: needs security review",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        })

    patches = _patch_list_profiles(["orchestrator", "agent007", "gm2"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee == "agent007"
    assert task.status == "triage"
    # Also verify no claim or other mutation leaked
    assert task.claim_lock is None
    assert task.claim_expires is None


def test_block_loop_skip_is_idempotent(kanban_home):
    """GREEN: Calling decompose_task twice on the same block-loop task
    returns the same skip outcome both times — no state change."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="hotfix", assignee="agent007", triage=True)
        _add_prior_run(conn, tid, outcome="done", summary="hotfix deployed")
        kb._append_event(conn, tid, "block_loop_detected", {
            "reason": "review-required: verify hotfix",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        })

    patches = _patch_list_profiles(["orchestrator", "agent007", "gm2"])
    for p in patches:
        p.start()
    try:
        outcome1 = decomp.decompose_task(tid, author="auto-decomposer")
        outcome2 = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome1.ok is False
    assert outcome2.ok is False
    assert "block-loop" in outcome1.reason.lower() or "prior execution" in outcome1.reason.lower()
    assert "block-loop" in outcome2.reason.lower() or "prior execution" in outcome2.reason.lower()

    # Task still intact after both calls
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.assignee == "agent007"
    # Still exactly one task in the system
    with kb.connect() as conn:
        all_tasks = kb.list_tasks(conn)
        assert len(all_tasks) == 1


def test_fresh_triage_intake_still_decomposes_normally(kanban_home):
    """GREEN: A genuinely new triage intake with NO block_loop_detected event
    and NO prior runs must still decompose normally — the guard must NOT
    block normal triage→decomposition flow."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="build notification system", triage=True)
        # No prior runs, no block_loop_detected event

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "normal triage intake",
        "tasks": [
            {"title": "design API", "body": "spec the endpoints", "assignee": "architect", "parents": []},
            {"title": "implement", "body": "code the server", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "architect", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, f"fresh triage should decompose normally: {outcome.reason}"
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        children = [kb.get_task(conn, cid) for cid in outcome.child_ids]
    assert root.status == "todo"
    assert all(c is not None for c in children)


def test_fresh_triage_with_no_block_loop_but_prior_runs_decomposes(kanban_home):
    """GREEN: A task in triage with prior runs but NO block_loop_detected
    event should still decompose — the guard is conjunctive (both required).
    This covers the case of a task that was manually moved to triage after
    prior execution but never went through the block-loop path."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="refactor cache layer", assignee="agent007", triage=True)
        _add_prior_run(conn, tid, outcome="done", summary="previous iteration")
        # No block_loop_detected event — just a prior run

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single cleanup",
        "title": "refactor cache layer v2",
        "body": "Do the refactor again.",
    })

    patches = _patch_list_profiles(["orchestrator", "agent007", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, (
        f"prior runs alone without block_loop_detected should still decompose: {outcome.reason}"
    )


def test_block_loop_without_prior_runs_still_decomposes(kanban_home):
    """GREEN: A task with a block_loop_detected event but NO prior runs
    should still decompose — it's a new task that just happened to hit
    the block-loop breaker (e.g., missing credential, repeated capability
    block for a task that was never executed)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="need new API key", triage=True)
        # No prior runs — this task was never executed
        kb._append_event(conn, tid, "block_loop_detected", {
            "reason": "capability: no API key configured",
            "kind": "capability",
            "recurrences": 2,
            "limit": 2,
        })

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single task",
        "title": "provision API key",
        "body": "Create a new API key for the service.",
    })

    patches = _patch_list_profiles(["orchestrator", "admin"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, (
        f"block_loop_detected without prior runs should still decompose: {outcome.reason}"
    )


def test_block_loop_skip_reason_is_descriptive(kanban_home):
    """GREEN: The skip reason must be descriptive enough for the gateway log
    and dashboard to surface why decomposition was skipped."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="migrate DB", assignee="agent007", triage=True)
        _add_prior_run(conn, tid, outcome="done", summary="migration completed")
        kb._append_event(conn, tid, "block_loop_detected", {
            "reason": "review-required: DB migration PR #900 ready",
            "kind": "needs_input",
            "recurrences": 2,
            "limit": 2,
        })

    patches = _patch_list_profiles(["orchestrator", "agent007"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "skipped" in outcome.reason.lower()
    assert "block-loop" in outcome.reason.lower()
    assert "prior execution" in outcome.reason.lower()
    assert "review" in outcome.reason.lower() or "audit" in outcome.reason.lower()
