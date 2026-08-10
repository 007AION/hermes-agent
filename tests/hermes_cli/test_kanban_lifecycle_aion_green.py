"""GREEN verification tests for AION-RL2-CORE-01 Native Kanban lifecycle repairs.

These tests verify the implemented behavior AFTER the RED→GREEN transition.
They build on the RED tests in test_kanban_lifecycle_aion.py.
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
# Workstream 1: Controller terminal projection — GREEN
# ═══════════════════════════════════════════════════════════════════════════


def test_controller_completes_triage_task(kanban_home):
    """GREEN: controller completion resolves triage → done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="triage-green", triage=True)
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert kb.complete_task(conn, tid, result="controlled done")
        done = kb.get_task(conn, tid)
        assert done.status == "done"
        assert done.result == "controlled done"


def test_controller_completes_todo_task(kanban_home):
    """GREEN: controller completion resolves todo → done."""
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
    """GREEN: controller completion resolves scheduled → done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="sched-green", assignee="a")
        kb.claim_task(conn, tid)
        ok = kb.schedule_task(conn, tid, reason="parked")
        assert ok
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.complete_task(conn, tid, result="controlled done")
        done = kb.get_task(conn, tid)
        assert done.status == "done"


def test_controller_completes_review_task(kanban_home):
    """GREEN: controller completion resolves review → done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review-green", assignee="a")
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
        tid = kb.create_task(conn, title="triage-cas-green", triage=True)
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


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 2: Board obligation diagnostics — GREEN
# ═══════════════════════════════════════════════════════════════════════════


def test_board_diagnostics_returns_status_counts(kanban_home):
    """Board diagnostics returns per-status counts."""
    with kb.connect() as conn:
        kb.create_task(conn, title="a", assignee="x")          # ready
        kb.create_task(conn, title="b", assignee="x")          # ready
        kb.create_task(conn, title="c", triage=True)            # triage
        diag = kd.compute_board_diagnostics(conn)
    assert diag["status_counts"]["ready"] == 2
    assert diag["status_counts"]["triage"] == 1


def test_board_diagnostics_executable_now(kanban_home):
    """executable_now counts only ready tasks."""
    with kb.connect() as conn:
        kb.create_task(conn, title="a", assignee="x")      # ready
        kb.create_task(conn, title="b", triage=True)        # triage
        parent = kb.create_task(conn, title="p", assignee="x")  # ready
        kb.create_task(conn, title="child", assignee="x", parents=[parent])  # todo
        diag = kd.compute_board_diagnostics(conn)
    assert diag["executable_now"] == 2  # "a" and "p" are ready
    assert diag["open_obligations"] == 4  # ready(2) + triage(1) + todo(1)


def test_board_diagnostics_executable_zero_open_nonzero(kanban_home):
    """When executable_now=0 but open_obligations>0, emit a hard finding."""
    with kb.connect() as conn:
        kb.create_task(conn, title="a", triage=True)  # triage
        kb.create_task(conn, title="b", triage=True)  # triage
        diag = kd.compute_board_diagnostics(conn)
    assert diag["executable_now"] == 0
    assert diag["open_obligations"] == 2
    assert len(diag["findings"]) >= 1
    finding = diag["findings"][0]
    assert finding["kind"] == "executable_zero_open_obligations"
    assert finding["severity"] == "error"
    nonterminal = finding["data"]["nonterminal_states"]
    assert nonterminal["triage"] == 2


def test_board_diagnostics_all_done_is_healthy(kanban_home):
    """No findings when everything is done."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="a", assignee="x")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="ok")
        diag = kd.compute_board_diagnostics(conn)
    assert diag["executable_now"] == 0
    assert diag["open_obligations"] == 0
    assert diag["findings"] == []


def test_board_diagnostics_flag_stale_triage(kanban_home):
    """A triage task older than threshold fires a stale_triage finding."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="old triage", triage=True)
        one_day_ago = int(time.time()) - 24 * 3600
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (one_day_ago, tid),
        )
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (one_day_ago, tid),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)
    stale = [f for f in diag["findings"] if f["kind"] == "stale_triage"]
    assert len(stale) == 1
    assert stale[0]["data"]["task_id"] == tid


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 3: Exact-workspace process closure — GREEN
# ═══════════════════════════════════════════════════════════════════════════


def test_close_workspace_processes_finds_workspace_child(tmp_path):
    """A process with cwd inside the workspace is identified and killed."""
    ws = tmp_path / "ws"
    ws.mkdir()
    proc = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=True,
    )
    try:
        time.sleep(0.1)
        result = kb.close_workspace_processes(ws)
        assert result["workspace"] == str(ws)
        assert result["signalled"] >= 1
        proc.wait(timeout=2)
        assert proc.returncode != 0  # killed by signal
    finally:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def test_close_workspace_processes_spares_outside_process(tmp_path):
    """An outside-workspace process must survive (negative canary)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    control = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(outside),
        start_new_session=True,
    )
    try:
        time.sleep(0.1)
        result = kb.close_workspace_processes(ws)
        assert control.poll() is None  # still alive
        assert result["workspace"] == str(ws)
        assert result["skipped_outside"] >= 1
    finally:
        try:
            control.kill()
            control.wait(timeout=2)
        except Exception:
            pass


def test_close_workspace_processes_dry_run_no_kill(tmp_path):
    """Dry-run mode reports what WOULD be killed without killing."""
    ws = tmp_path / "ws"
    ws.mkdir()
    proc = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=True,
    )
    try:
        time.sleep(0.1)
        result = kb.close_workspace_processes(ws, dry_run=True)
        assert result["dry_run"] is True
        assert result["would_signal"] >= 1
        assert proc.poll() is None  # still alive
    finally:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def test_close_workspace_processes_nonexistent_dir(tmp_path):
    """No error when workspace directory doesn't exist."""
    result = kb.close_workspace_processes(tmp_path / "nonexistent")
    assert result["signalled"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 4: Spawn EAGAIN classification — GREEN
# ═══════════════════════════════════════════════════════════════════════════


def test_classify_failure_eagain_is_platform_resource():
    """EAGAIN/Errno 11 is classified as platform_resource."""
    assert kb._classify_failure(
        "OSError: [Errno 11] Resource temporarily unavailable"
    ) == "platform_resource"
    assert kb._classify_failure("subprocess: EAGAIN during fork") == "platform_resource"
    assert kb._classify_failure("Resource temporarily unavailable") == "platform_resource"


def test_classify_failure_enomem_is_platform_resource():
    """ENOMEM/Errno 12 is classified as platform_resource."""
    assert kb._classify_failure(
        "OSError: [Errno 12] Cannot allocate memory"
    ) == "platform_resource"
    assert kb._classify_failure("cannot allocate memory") == "platform_resource"


def test_classify_failure_enospc_is_platform_resource():
    """ENOSPC/Errno 28 is classified as platform_resource."""
    assert kb._classify_failure(
        "OSError: [Errno 28] No space left on device"
    ) == "platform_resource"


def test_classify_failure_normal_error_is_task():
    """Normal errors are classified as task."""
    assert kb._classify_failure("Profile 'x' does not exist") == "task"
    assert kb._classify_failure("something went wrong") == "task"


def test_spawn_failure_records_failure_category(kanban_home):
    """_record_spawn_failure includes failure_category in gave_up event."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, tid)
        kb._record_spawn_failure(
            conn, tid, "OSError: [Errno 11] Resource temporarily unavailable",
            failure_limit=1,
        )
        events = kb.list_events(conn, tid)
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert len(gave_up) == 1
        payload = gave_up[0].payload
        assert payload is not None
        assert payload.get("failure_category") == "platform_resource"


def test_spawn_failure_task_error_has_task_category(kanban_home):
    """Normal task errors get failure_category='task'."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="a")
        kb.claim_task(conn, tid)
        kb._record_spawn_failure(
            conn, tid, "Profile 'ghost' does not exist",
            failure_limit=1,
        )
        events = kb.list_events(conn, tid)
        gave_up = [e for e in events if e.kind == "gave_up"]
        assert len(gave_up) == 1
        assert gave_up[0].payload.get("failure_category") == "task"
