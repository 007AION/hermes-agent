"""Regression coverage for transient retries of typed review-handoff children."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated board for the review-child lifecycle fixture."""
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


def _review_child_after_legal_unblock(conn):
    author = kb.create_task(
        conn,
        title="implementation",
        assignee="author",
        workspace_kind="dir",
        workspace_path="/tmp/exact-review-workspace",
    )
    author_run = kb.claim_task(conn, author)
    assert author_run is not None and author_run.current_run_id is not None
    review_task = kb.create_task(
        conn,
        title="independent audit",
        assignee="auditor",
        parents=[author],
        workspace_kind="dir",
        workspace_path="/tmp/exact-review-workspace",
        provider_override="openai-codex",
        model_override="gpt-5.6-sol",
    )
    receipt = kb.request_review_handoff(
        conn,
        author,
        expected_run_id=author_run.current_run_id,
        review_task_id=review_task,
        reason="candidate frozen for independent audit",
    )
    assert receipt is not None

    first_review_run = kb.claim_task(conn, review_task, claimer="host:first-review")
    assert first_review_run is not None and first_review_run.current_run_id is not None
    first_review_run_id = first_review_run.current_run_id
    assert kb.block_task(
        conn,
        review_task,
        reason="provider failure before substantive audit output",
        kind="transient",
        expected_run_id=first_review_run_id,
    )
    assert kb.unblock_task(conn, review_task)
    assert _task(conn, review_task).status == "todo"
    return author, receipt.expected_run_id, review_task, first_review_run_id, receipt


def _identity_snapshot(conn, author, review_task, first_review_run_id):
    task = conn.execute(
        "SELECT id, assignee, workspace_kind, workspace_path, provider_override, "
        "model_override, block_kind, block_recurrences FROM tasks WHERE id = ?",
        (review_task,),
    ).fetchone()
    run = conn.execute(
        "SELECT id, task_id, profile, status, outcome, ended_at FROM task_runs WHERE id = ?",
        (first_review_run_id,),
    ).fetchone()
    links = conn.execute(
        "SELECT parent_id, child_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (review_task,),
    ).fetchall()
    receipt_events = conn.execute(
        "SELECT id, task_id, run_id, kind, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'review_handoff' ORDER BY id",
        (author,),
    ).fetchall()
    counts = tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("tasks", "task_runs", "task_links")
    )
    return (
        tuple(task),
        tuple(run),
        tuple(tuple(row) for row in links),
        tuple(tuple(row) for row in receipt_events),
        counts,
    )


def test_recompute_ready_restores_same_unblocked_review_child(kanban_home):
    """A valid typed handoff remains a satisfied parent after one legal retry."""
    with kb.connect() as conn:
        author, _, review_task, first_run_id, receipt = (
            _review_child_after_legal_unblock(conn)
        )
        before = _identity_snapshot(conn, author, review_task, first_run_id)
        promoted_before = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'promoted'",
            (review_task,),
        ).fetchone()[0]

        assert kb.recompute_ready(conn) == 1
        ready = _task(conn, review_task)
        assert ready.status == "ready"
        assert ready.current_run_id is None
        assert _identity_snapshot(conn, author, review_task, first_run_id) == before
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'promoted'",
            (review_task,),
        ).fetchone()[0] == promoted_before + 1

        retried = kb.claim_task(conn, review_task, claimer="host:bounded-retry")
        assert retried is not None
        assert retried.id == review_task
        assert retried.current_run_id != first_run_id
        old_run = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE id = ?", (first_run_id,)
        ).fetchone()
        assert tuple(old_run) == ("blocked", "blocked")
        assert len(conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'review_handoff'",
            (author,),
        ).fetchall()) == 1
        assert receipt.review_task_id == review_task


@pytest.mark.parametrize(
    "invalidity",
    [
        "missing_receipt",
        "forged_receipt",
        "mismatched_child",
        "stale_author_run",
        "self_audit",
        "active_author_identity",
        "active_child_identity",
        "unrelated_open_parent",
    ],
)
def test_recompute_ready_rejects_invalid_review_parent_without_mutation(
    kanban_home, invalidity,
):
    with kb.connect() as conn:
        author, author_run_id, review_task, first_run_id, _ = (
            _review_child_after_legal_unblock(conn)
        )
        event = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'review_handoff'",
            (author,),
        ).fetchone()

        if invalidity == "missing_receipt":
            conn.execute("DELETE FROM task_events WHERE id = ?", (event["id"],))
        elif invalidity == "forged_receipt":
            payload = json.loads(event["payload"])
            payload["receipt_sha256"] = "0" * 64
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(payload), event["id"]),
            )
        elif invalidity == "mismatched_child":
            payload = json.loads(event["payload"])
            payload["review_task_id"] = "t_wrongchild"
            signed = {
                "task_id": author,
                **{key: payload[key] for key in (
                    "version", "expected_run_id", "review_task_id", "reason", "recovery"
                )},
            }
            payload["receipt_sha256"] = hashlib.sha256(
                json.dumps(
                    signed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(payload), event["id"]),
            )
        elif invalidity == "stale_author_run":
            conn.execute(
                "UPDATE task_runs SET status = 'blocked', outcome = 'blocked' WHERE id = ?",
                (author_run_id,),
            )
        elif invalidity == "self_audit":
            conn.execute(
                "UPDATE tasks SET assignee = 'author' WHERE id = ?", (review_task,)
            )
        elif invalidity == "active_author_identity":
            conn.execute(
                "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                (author_run_id, author),
            )
        elif invalidity == "active_child_identity":
            conn.execute(
                "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                (first_run_id, review_task),
            )
        else:
            other = kb.create_task(conn, title="open prerequisite", assignee="builder")
            kb.link_tasks(conn, other, review_task)
        conn.commit()

        before = "\n".join(conn.iterdump())
        assert kb.recompute_ready(conn) == 0
        assert _task(conn, review_task).status == "todo"
        assert "\n".join(conn.iterdump()) == before
        assert kb.claim_task(conn, review_task) is None
        assert _task(conn, review_task).status == "todo"
        old_run = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE id = ?", (first_run_id,)
        ).fetchone()
        assert tuple(old_run) == ("blocked", "blocked")


@pytest.mark.parametrize("identity", ("author", "child"))
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("current_run_id", 999999),
        ("claim_lock", "host:unexpected-active"),
        ("claim_expires", 9999999999),
        ("worker_pid", 424242),
        ("worker_starttime", 31337),
        ("fence_lineage", "unexpected-lineage"),
        ("fence_disposition", "unexpected-disposition"),
    ),
)
def test_recompute_ready_rejects_each_persisted_execution_identity(
    kanban_home, identity, field, value,
):
    """No single stale execution/fence field may create a runnable duplicate."""
    with kb.connect() as conn:
        author, _, review_task, first_run_id, _ = (
            _review_child_after_legal_unblock(conn)
        )
        target = author if identity == "author" else review_task
        conn.execute(f"UPDATE tasks SET {field} = ? WHERE id = ?", (value, target))
        conn.commit()

        before = "\n".join(conn.iterdump())
        assert kb.recompute_ready(conn) == 0
        assert _task(conn, review_task).status == "todo"
        assert "\n".join(conn.iterdump()) == before
        assert kb.claim_task(conn, review_task) is None
        assert _task(conn, review_task).status == "todo"
        old_run = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE id = ?", (first_run_id,)
        ).fetchone()
        assert tuple(old_run) == ("blocked", "blocked")


def test_review_child_retry_is_concurrent_safe_and_recurrence_bounded(kanban_home):
    with kb.connect() as conn:
        author, _, review_task, first_run_id, _ = _review_child_after_legal_unblock(conn)
        counts_before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_runs", "task_links")
        )
        promoted_before = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'promoted'",
            (review_task,),
        ).fetchone()[0]

    barrier = threading.Barrier(2)

    def promote():
        with kb.connect() as thread_conn:
            barrier.wait()
            return kb.recompute_ready(thread_conn)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: promote(), range(2)))
    assert sorted(results) == [0, 1]

    with kb.connect() as conn:
        assert _task(conn, review_task).status == "ready"
        assert tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "task_runs", "task_links")
        ) == counts_before
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'promoted'",
            (review_task,),
        ).fetchone()[0] == promoted_before + 1

        retry = kb.claim_task(conn, review_task, claimer="host:retry")
        assert retry is not None and retry.current_run_id is not None
        assert retry.current_run_id != first_run_id
        assert kb.block_task(
            conn,
            review_task,
            reason="provider failure before substantive audit output",
            kind="transient",
            expected_run_id=retry.current_run_id,
        )
        escalated = _task(conn, review_task)
        assert escalated.status == "triage"
        assert escalated.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        assert kb.recompute_ready(conn) == 0
        assert _task(conn, review_task).status == "triage"
        assert len(conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'review_handoff'",
            (author,),
        ).fetchall()) == 1
