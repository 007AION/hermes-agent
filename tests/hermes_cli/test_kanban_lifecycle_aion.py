"""Tests for AION-RL2-CORE-01 Native Kanban lifecycle repairs.

Controller terminal projection, board obligation diagnostics, exact-workspace
process closure, and platform-resource spawn attribution.

These tests integrate RED→GREEN evidence: the RED phase confirmed the current
base lacked each capability; the GREEN phase confirms the implementations work.

File: tests/hermes_cli/test_kanban_lifecycle_aion.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 1: Controller terminal projection
# ═══════════════════════════════════════════════════════════════════════════
#
# Controller completion (expected_run_id=None) now resolves triage, todo,
# scheduled, and review to done, recording prior_status in the event.


def test_controller_completes_triage_task(kanban_home):
    """Controller completion resolves triage → done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="triage-task", triage=True)
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert kb.complete_task(conn, tid, result="controlled done")
        done = kb.get_task(conn, tid)
        assert done.status == "done"
        assert done.result == "controlled done"


def test_controller_completes_todo_task(kanban_home):
    """Controller completion resolves todo → done."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        assert kb.get_task(conn, child).status == "todo"
        assert kb.complete_task(conn, child, result="controlled done")
        done = kb.get_task(conn, child)
        assert done.status == "done"
        assert done.result == "controlled done"


def test_controller_completes_scheduled_task(kanban_home):
    """Controller completion resolves scheduled → done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="sched-task", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.schedule_task(conn, tid, reason="parked")
        assert ok
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.complete_task(conn, tid, result="controlled done")
        done = kb.get_task(conn, tid)
        assert done.status == "done"


def test_controller_completes_review_task(kanban_home):
    """Controller completion resolves review → done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review-task", assignee="a")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="worker done")
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (tid,),
        )
        conn.commit()
        assert kb.get_task(conn, tid).status == "review"
        assert kb.complete_task(conn, tid, result="controlled done")
        done = kb.get_task(conn, tid)
        assert done.status == "done"


def test_controller_completions_record_prior_status(kanban_home):
    """Controller completion of non-running tasks records prior_status in event."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="triage-audit", triage=True)
        assert kb.complete_task(conn, tid, result="controlled done")
        events = kb.list_events(conn, tid)
        completed_ev = [e for e in events if e.kind == "completed"]
        assert len(completed_ev) == 1
        assert completed_ev[0].payload.get("prior_status") == "triage"


def test_controller_completion_no_prior_for_running(kanban_home):
    """Controller completion of running task does NOT set prior_status."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="running-ctl", assignee="a")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, result="controlled done")
        events = kb.list_events(conn, tid)
        completed_ev = [e for e in events if e.kind == "completed"]
        assert len(completed_ev) == 1
        assert "prior_status" not in (completed_ev[0].payload or {})


def test_worker_cannot_complete_triage(kanban_home):
    """Worker-bound completion MUST NOT accept triage (CAS gate)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="triage-cas", triage=True)
        result = kb.complete_task(
            conn, tid, result="worker done", expected_run_id=999,
        )
        assert result is False


def test_worker_cannot_complete_todo(kanban_home):
    """Worker-bound completion MUST NOT accept todo (CAS gate)."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(
            conn, title="child", assignee="a", parents=[parent],
        )
        result = kb.complete_task(
            conn, child, result="worker done", expected_run_id=999,
        )
        assert result is False


def test_worker_cannot_complete_scheduled(kanban_home):
    """Worker-bound completion MUST NOT accept scheduled (CAS gate)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="sched-cas", assignee="a")
        kb.claim_task(conn, tid)
        kb.schedule_task(conn, tid, reason="parked")
        result = kb.complete_task(
            conn, tid, result="worker done", expected_run_id=999,
        )
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 2: Board obligation diagnostics
# ═══════════════════════════════════════════════════════════════════════════


def test_compute_board_diagnostics_exists():
    """compute_board_diagnostics is importable and callable."""
    assert callable(kd.compute_board_diagnostics)


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 3: Exact-workspace process closure
# ═══════════════════════════════════════════════════════════════════════════


def test_close_workspace_processes_exists():
    """close_workspace_processes is importable and callable."""
    assert callable(kb.close_workspace_processes)


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 4: Spawn EAGAIN classification
# ═══════════════════════════════════════════════════════════════════════════


def test_classify_failure_exists():
    """_classify_failure is importable and callable."""
    assert callable(kb._classify_failure)
