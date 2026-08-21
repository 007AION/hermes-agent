"""Regression coverage for the DB-level factory terminal-write fence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _db_path(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA database_list").fetchone()[2])


def _legacy_worker_complete(conn: sqlite3.Connection, task_id: str, run_id: int) -> None:
    """Exact terminal UPDATE used by the pre-gate installed complete_task path."""
    conn.execute(
        """
        UPDATE tasks
           SET status = 'done', completed_at = 1,
               claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
         WHERE id = ?
           AND status IN ('running', 'ready', 'blocked')
           AND current_run_id = ?
        """,
        (task_id, run_id),
    )
    conn.commit()


def _legacy_controller_complete(conn: sqlite3.Connection, task_id: str) -> None:
    """Controller/manual equivalent of the pre-gate installed terminal UPDATE."""
    conn.execute(
        "UPDATE tasks SET status = 'done', completed_at = 1 WHERE id = ? AND status = 'ready'",
        (task_id,),
    )
    conn.commit()


@pytest.mark.parametrize("surface", ["worker", "controller"])
def test_stale_completion_connection_cannot_bypass_factory_receipt_gate(
    kanban_home, surface: str,
):
    """A pre-gate installed module must fail closed after the schema fence lands.

    The stale connection deliberately uses sqlite3 directly, so it never
    creates the transaction-local authorization row used by current
    kanban_db. This reproduces run3015's installed worker/controller bypass
    without modifying production state.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="factory terminal fence", factory_build_gate=1,
        )
        run_id = None
        if surface == "worker":
            assert kb.claim_task(conn, task_id) is not None
            task = kb.get_task(conn, task_id)
            assert task is not None
            run_id = task.current_run_id
            assert run_id is not None
        db_path = _db_path(conn)

    stale = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="authenticated receipt"):
            if surface == "worker":
                assert run_id is not None
                _legacy_worker_complete(stale, task_id, run_id)
            else:
                _legacy_controller_complete(stale, task_id)
    finally:
        stale.close()

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == ("running" if surface == "worker" else "ready")
        receipt = conn.execute(
            "SELECT factory_terminal_receipt_sha256 FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert receipt["factory_terminal_receipt_sha256"] is None
        assert kb.list_attachments(conn, task_id) == []


def test_ungranted_current_connection_cannot_directly_terminalize_factory_task(
    kanban_home,
):
    """A current connection also denies direct writes without a validated grant."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="factory terminal fence", factory_build_gate=1,
        )
        with pytest.raises(sqlite3.IntegrityError, match="authenticated receipt"):
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (task_id,),
            )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"


def test_stale_connection_preserves_non_factory_completion_parity(kanban_home):
    """The terminal-write fence is irrelevant for gate=0 tasks."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy non-factory task")
        db_path = _db_path(conn)

    stale = sqlite3.connect(db_path)
    try:
        _legacy_controller_complete(stale, task_id)
    finally:
        stale.close()

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"
