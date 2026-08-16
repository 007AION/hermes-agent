"""Tests for R29: recovering a block-loop-origin triage task via ``unblock_task``.

The block-loop breaker in :func:`hermes_cli.kanban_db.block_task` routes a
task that has been blocked, unblocked, and re-blocked for the SAME cause
``BLOCK_RECURRENCE_LIMIT`` times into ``triage`` (instead of ``blocked``) and
emits a ``block_loop_detected`` event. That triage state is a deliberate
human-in-the-loop decision — the loop is broken, but the SAME canonical task
still needs to be resumable once the underlying capability gate is satisfied.

Before R29, the orchestrator-only ``kanban_unblock`` operation
(``unblock_task``) could only transition ``blocked``/``scheduled`` tasks, so a
block-loop-origin triage task was unrecoverable through any exposed safe
surface (the ``block_loop_detected`` event is the *only* machine-readable
provenance that this triage came from the loop breaker, not from ordinary
intake/specification).

The fix extends ``unblock_task`` with a narrow, provenance-gated branch:

* a triage task that has a ``block_loop_detected`` event is parent-gated and
  promoted to ``ready`` (all parents done/archived) or ``todo`` (a parent
  still open), with ``block_recurrences``/``block_kind`` reset (fresh start)
  and an auditable ``recovered_triage`` event appended;
* any OTHER triage task (ordinary intake/specifier, no block-loop
  provenance) remains rejected — fail closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


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


def _block_loop_triage(conn, kind="capability"):
    """Drive a task through block → unblock → same-cause re-block to triage."""
    tid = _running_task(conn)
    kb.block_task(conn, tid, reason="need X", kind=kind)
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="still need X", kind=kind)
    t = kb.get_task(conn, tid)
    assert t.status == "triage", f"expected triage, got {t.status}"
    return tid


def _done_parent(conn):
    """Create and complete a parent task, returning its id."""
    parent = kb.create_task(conn, title="parent", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
    assert kb.claim_task(conn, parent, claimer="worker") is not None
    assert kb.complete_task(conn, parent, result="done")
    return parent


# ---------------------------------------------------------------------------
# GREEN — recovery of a block-loop-origin triage task
# ---------------------------------------------------------------------------


def test_block_loop_triage_recovers_to_ready_when_parent_free(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _block_loop_triage(conn)
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        # auditable recovery event with provenance
        events = [e for e in kb.list_events(conn, tid) if e.kind == "recovered_triage"]
        assert events
        assert events[-1].payload == {"from": "block_loop_detected", "status": "ready"}


def test_block_loop_triage_recovers_to_ready_when_all_parents_done(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _block_loop_triage(conn)
        parent = _done_parent(conn)
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"


def test_block_loop_triage_recovers_to_todo_when_parent_open(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _block_loop_triage(conn)  # no parents at loop time
        open_parent = kb.create_task(conn, title="open parent", assignee="worker")
        kb.link_tasks(conn, parent_id=open_parent, child_id=tid)
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "todo", "open parent must keep the task in todo, not ready"


def test_recovery_resets_block_memory(kanban_home: Path) -> None:
    """A deliberate recovery is a fresh start: loop counter and kind reset."""
    with kb.connect_closing() as conn:
        tid = _block_loop_triage(conn)
        assert kb.get_task(conn, tid).block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.block_recurrences == 0
        assert t.block_kind is None


def test_recovery_is_idempotent(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _block_loop_triage(conn)

        def recovered_count() -> int:
            return len(
                [e for e in kb.list_events(conn, tid) if e.kind == "recovered_triage"]
            )

        assert kb.unblock_task(conn, tid)
        assert recovered_count() == 1
        # Second call: no longer triage — returns False, no extra event, no mutation.
        assert not kb.unblock_task(conn, tid)
        assert recovered_count() == 1
        assert kb.get_task(conn, tid).status == "ready"


def test_recovery_preserves_identity_comments_runs_edges(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        parent = _done_parent(conn)
        tid = _block_loop_triage(conn)
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        kb.add_comment(conn, tid, "gm2", "wake comment")

        runs_before = kb.list_runs(conn, tid)
        assert runs_before, "expected a closed run"
        assert runs_before[-1].status == "blocked"
        assert runs_before[-1].ended_at is not None
        comments_before = kb.list_comments(conn, tid)
        assert comments_before

        assert kb.unblock_task(conn, tid)

        t = kb.get_task(conn, tid)
        assert t.id == tid
        assert t.status == "ready"
        # Run history preserved: same run rows, the closed 'blocked' run is
        # untouched (NOT reclaimed/completed) by recovery.
        runs_after = kb.list_runs(conn, tid)
        assert len(runs_after) == len(runs_before)
        assert runs_after[-1].status == "blocked"
        assert runs_after[-1].ended_at is not None
        # Comments and edges preserved.
        assert len(kb.list_comments(conn, tid)) == len(comments_before)
        assert parent in kb.parent_ids(conn, tid)
        # Block-loop provenance event still present alongside the recovery event.
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "block_loop_detected" in kinds
        assert "recovered_triage" in kinds


# ---------------------------------------------------------------------------
# Negative — ordinary triage stays fail-closed; terminals stay immutable
# ---------------------------------------------------------------------------


def test_ordinary_intake_triage_rejected(kanban_home: Path) -> None:
    """A triage task without block-loop provenance must NOT be unblockable."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="intake", triage=True)
        assert kb.get_task(conn, tid).status == "triage"
        assert not kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "triage"
        assert not any(e.kind == "recovered_triage" for e in kb.list_events(conn, tid))


def test_triage_with_block_history_but_no_loop_rejected(kanban_home: Path) -> None:
    """The predicate is specifically ``block_loop_detected`` provenance — a
    task that merely has blocked/unblocked events does not qualify."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="sticky", triage=True)
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "blocked",
                {"reason": "r", "kind": "capability", "recurrences": 1},
            )
            kb._append_event(conn, tid, "unblocked", {"status": "ready"})
        assert kb.get_task(conn, tid).status == "triage"
        assert not kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "triage"


def test_terminal_statuses_remain_immutable(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        done = kb.create_task(conn, title="done", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (done,))
        kb.claim_task(conn, done, claimer="worker")
        kb.complete_task(conn, done, result="done")
        assert kb.get_task(conn, done).status == "done"
        assert not kb.unblock_task(conn, done)
        assert kb.get_task(conn, done).status == "done"


def test_normal_blocked_unblock_unchanged(kanban_home: Path) -> None:
    """The pre-existing blocked/scheduled path is unaffected by the triage branch."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="needs_input")
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        # No recovery event on the ordinary path.
        assert not any(e.kind == "recovered_triage" for e in kb.list_events(conn, tid))
