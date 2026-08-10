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
from unittest.mock import patch

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


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 3b: Completion-triggered workspace process closure — INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════
# These tests prove that complete_task → _cleanup_workspace → close_workspace_processes
# closes child/grandchild processes inside the workspace while preserving outside
# processes and the current process itself (#AION-RL2-CORE-01 repair).


def test_completion_closes_workspace_child_process(kanban_home, tmp_path):
    """complete_task closes a child process whose cwd is inside the workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=True,
    )
    try:
        time.sleep(0.1)
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="proc-test", assignee="a")
            # Set workspace to the dir where child runs.
            conn.execute(
                "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                (str(ws), tid),
            )
            conn.commit()
            kb.claim_task(conn, tid)
            assert kb.complete_task(conn, tid, result="done")

        # Child should be killed by workspace process closure.
        child.wait(timeout=5)
        assert child.returncode != 0  # killed by signal
    finally:
        try:
            child.kill()
            child.wait(timeout=2)
        except Exception:
            pass


def test_completion_preserves_outside_process(kanban_home, tmp_path):
    """complete_task preserves a process whose cwd is outside the workspace."""
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
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="outside-test", assignee="a")
            conn.execute(
                "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                (str(ws), tid),
            )
            conn.commit()
            kb.claim_task(conn, tid)
            assert kb.complete_task(conn, tid, result="done")

        # Outside process must survive.
        assert control.poll() is None
    finally:
        try:
            control.kill()
            control.wait(timeout=2)
        except Exception:
            pass


def test_completion_preserves_self_process(kanban_home, tmp_path):
    """complete_task never signals the current process (self-preservation)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    import os as _os
    my_pid = _os.getpid()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="self-test", assignee="a")
        conn.execute(
            "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
            (str(tmp_path), tid),  # workspace covers our own cwd
        )
        conn.commit()
        kb.claim_task(conn, tid)
        # Must not kill ourselves.
        assert kb.complete_task(conn, tid, result="done")
    # We're still alive.
    assert _os.getpid() == my_pid


def test_close_workspace_processes_preserves_caller_inside_workspace(tmp_path):
    """close_workspace_processes skips caller PID/PGID when caller CWD is inside workspace.

    Per bafuxunan audit (t_4d4f44ac): the previous self-preservation test
    was insufficient because the caller's CWD was outside the workspace.
    This test proves that when the caller IS inside the workspace,
    close_workspace_processes() still does not signal it.
    """
    ws = tmp_path / "ws"
    ws.mkdir()

    # Spawn a child+grandchild inside the workspace (separate process groups).
    child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=True,
    )
    grandchild = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(str(ws) + "/"),
        start_new_session=True,
    )
    # Outside negative control.
    outside = tmp_path / "outside"
    outside.mkdir()
    control = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(outside),
        start_new_session=True,
    )
    try:
        time.sleep(0.15)

        # Change caller CWD INTO the workspace — this is the critical difference
        # from the previous test_completion_preserves_self_process.
        old_cwd = os.getcwd()
        try:
            os.chdir(str(ws))
            result = kb.close_workspace_processes(ws, dry_run=False)
        finally:
            os.chdir(old_cwd)

        # Caller survived — we are still here.
        assert result["skipped_self"] >= 1

        # Children inside workspace were signalled.
        assert result["signalled"] >= 2

        # Child and grandchild are dead.
        child.wait(timeout=5)
        grandchild.wait(timeout=5)
        assert child.returncode != 0
        assert grandchild.returncode != 0

        # Outside control survived.
        assert control.poll() is None
    finally:
        for p in [child, grandchild, control]:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


def test_completion_preserves_caller_inside_workspace(kanban_home, tmp_path):
    """complete_task with caller CWD inside workspace closes children, preserves caller.

    Per bafuxunan audit (t_4d4f44ac): the complete_task path must also survive
    when the caller's CWD is inside the exact workspace, closing child+grandchild
    while preserving the caller and outside negative controls.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    import os as _os
    my_pid = _os.getpid()

    # Spawn child+grandchild inside workspace.
    child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=True,
    )
    grandchild = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(str(ws) + "/"),
        start_new_session=True,
    )
    # Outside negative control.
    control = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(outside),
        start_new_session=True,
    )
    try:
        time.sleep(0.15)

        # Change caller CWD INTO the workspace.
        old_cwd = os.getcwd()
        try:
            os.chdir(str(ws))
            with kb.connect() as conn:
                tid = kb.create_task(conn, title="caller-in-ws", assignee="a")
                conn.execute(
                    "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                    (str(ws), tid),
                )
                conn.commit()
                kb.claim_task(conn, tid)
                # complete_task triggers _cleanup_workspace which calls
                # close_workspace_processes — caller is inside the workspace
                # so self-preservation must work.
                assert kb.complete_task(conn, tid, result="done")
            # Caller survived completion.
            assert _os.getpid() == my_pid
        finally:
            os.chdir(old_cwd)

        # Children were killed.
        child.wait(timeout=5)
        grandchild.wait(timeout=5)
        assert child.returncode != 0
        assert grandchild.returncode != 0

        # Outside control survived.
        assert control.poll() is None
    finally:
        for p in [child, grandchild, control]:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


def test_close_workspace_processes_caller_same_pgid_children_closed(tmp_path):
    """Children sharing the caller's PGID are PID-scoped signalled; caller survives.

    Per bafuxunan audit (t_e0b0681f at head 27a330d4): do not skip the entire
    current PGID. When children share the caller's PGID (no start_new_session),
    each eligible child is signalled by PID — never killpg(current_pgid).
    Caller exits 0, children actually close, outside negative control survives.
    """
    ws = tmp_path / "ws"
    ws.mkdir()

    # Children that SHARE the caller's PGID (no start_new_session).
    child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=False,
    )
    grandchild = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(str(ws) + "/"),
        start_new_session=False,
    )
    # Outside negative control — starts its own session.
    outside = tmp_path / "outside"
    outside.mkdir()
    control = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(outside),
        start_new_session=True,
    )
    try:
        time.sleep(0.15)

        # Change caller CWD INTO the workspace.
        old_cwd = os.getcwd()
        try:
            os.chdir(str(ws))
            result = kb.close_workspace_processes(ws, dry_run=False)
        finally:
            os.chdir(old_cwd)

        # Caller survived — we are still here.
        assert result["skipped_self"] >= 1

        # Children inside workspace were signalled by PID (not via killpg).
        assert result["signalled"] >= 2

        # Child and grandchild are dead.
        child.wait(timeout=5)
        grandchild.wait(timeout=5)
        assert child.returncode != 0
        assert grandchild.returncode != 0

        # Outside control survived.
        assert control.poll() is None
    finally:
        for p in [child, grandchild, control]:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


def test_completion_caller_same_pgid_children_closed(kanban_home, tmp_path):
    """complete_task with same-PGID children: PID-scoped signals, caller survives.

    Per bafuxunan audit (t_e0b0681f at head 27a330d4): the complete_task path
    must close same-PGID children by PID while preserving the caller and outside
    negative controls.  Children inherit caller PGID (no start_new_session).
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    import os as _os
    my_pid = _os.getpid()

    # Spawn children that SHARE the caller's PGID.
    child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=False,
    )
    grandchild = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(str(ws) + "/"),
        start_new_session=False,
    )
    # Outside negative control in its own session.
    control = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(outside),
        start_new_session=True,
    )
    try:
        time.sleep(0.15)

        # Change caller CWD INTO the workspace.
        old_cwd = os.getcwd()
        try:
            os.chdir(str(ws))
            with kb.connect() as conn:
                tid = kb.create_task(conn, title="same-pgid-completion", assignee="a")
                conn.execute(
                    "UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                    (str(ws), tid),
                )
                conn.commit()
                kb.claim_task(conn, tid)
                # complete_task triggers _cleanup_workspace → close_workspace_processes.
                # Caller is inside workspace and shares PGID with children.
                assert kb.complete_task(conn, tid, result="done")
            # Caller survived completion.
            assert _os.getpid() == my_pid
        finally:
            os.chdir(old_cwd)

        # Children were killed.
        child.wait(timeout=5)
        grandchild.wait(timeout=5)
        assert child.returncode != 0
        assert grandchild.returncode != 0

        # Outside control survived.
        assert control.poll() is None
    finally:
        for p in [child, grandchild, control]:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 2b: Blocked-parent wake mismatch + stale obligation diagnostics
# ═══════════════════════════════════════════════════════════════════════════


def test_blocked_parent_wake_mismatch_finding(kanban_home):
    """A child in todo/blocked with all parents done fires blocked_parent_wake_mismatch."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="done-parent", assignee="a")
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="ok")
        # recompute_ready has already promoted the child. Force it back to
        # blocked so the mismatch diagnostic fires (simulates a recompute
        # bug or missed promotion).
        child = kb.create_task(
            conn, title="orphaned-child", assignee="a", parents=[parent],
        )
        # The child was promoted to ready by recompute_ready. Force it back.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'dependency' "
            "WHERE id = ?", (child,),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)
    wake = [f for f in diag["findings"] if f["kind"] == "blocked_parent_wake_mismatch"]
    assert len(wake) >= 1
    assert wake[0]["data"]["task_id"] == child


def test_stale_scheduled_finding(kanban_home):
    """A scheduled task older than threshold fires stale_scheduled."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="parked-task", assignee="a")
        kb.claim_task(conn, tid)
        kb.schedule_task(conn, tid, reason="waiting")
        one_week_ago = int(time.time()) - 7 * 24 * 3600
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (one_week_ago, tid),
        )
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (one_week_ago, tid),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)
    stale = [f for f in diag["findings"] if f["kind"] == "stale_scheduled"]
    assert len(stale) >= 1
    assert stale[0]["data"]["task_id"] == tid


def test_stale_review_finding(kanban_home):
    """A review task older than threshold fires stale_review."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review-parked", assignee="a")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="worker done")
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (tid,),
        )
        one_week_ago = int(time.time()) - 7 * 24 * 3600
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (one_week_ago, tid),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)
    stale = [f for f in diag["findings"] if f["kind"] == "stale_review"]
    assert len(stale) >= 1
    assert stale[0]["data"]["task_id"] == tid


# ═══════════════════════════════════════════════════════════════════════════
# Workstream 3c: Process identity TOCTOU protection — GREEN
# ═══════════════════════════════════════════════════════════════════════════
# Per bafuxunan audit (t_926703bb at head 8d67916a): capture /proc/<pid>/stat
# starttime and re-read identity + cwd containment + PGID immediately before
# every signal.  Never signal on mismatch.  Mixed groups must not broad-signal
# protected/outside members.


def test_identity_mismatch_skips_signal_same_pgid(tmp_path):
    """PID-scope signal is skipped when identity re-read fails.

    When _revalidate_identity returns False for a same-PGID child
    (simulating PID reuse, cwd change, or pgid change), the signal
    is withheld and skipped_identity_mismatch is incremented.  The
    child survives.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=False,  # same PGID as caller
    )
    try:
        time.sleep(0.1)
        # Monkeypatch _revalidate_identity to always return False —
        # simulates identity mismatch after capture.
        with patch(
            "hermes_cli.kanban_db._revalidate_identity", return_value=False,
        ):
            result = kb.close_workspace_processes(ws)
        # Identity mismatch counter incremented.
        assert result["skipped_identity_mismatch"] >= 1
        # Child was NOT signalled — it is still alive.
        assert child.poll() is None
    finally:
        try:
            child.kill()
            child.wait(timeout=2)
        except Exception:
            pass


def test_identity_mismatch_skips_signal_diff_pgid(tmp_path):
    """killpg is skipped when identity re-read fails for a different-PGID group.

    Same as above but for processes in a different process group
    (start_new_session=True).  The group leader identity must match
    before killpg is called.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=True,  # different PGID
    )
    try:
        time.sleep(0.1)
        with patch(
            "hermes_cli.kanban_db._revalidate_identity", return_value=False,
        ):
            result = kb.close_workspace_processes(ws)
        assert result["skipped_identity_mismatch"] >= 1
        # Child was NOT signalled.
        assert child.poll() is None
    finally:
        try:
            child.kill()
            child.wait(timeout=2)
        except Exception:
            pass


def test_mixed_pgids_only_signal_eligible(tmp_path):
    """Mixed PGIDs: same-PGID children signalled by PID; outside/unmatched skip.

    When some children share the caller's PGID and others are in different
    groups, only eligible in-workspace processes are signalled.  Outside
    processes survive.  This proves mixed groups do not broad-signal
    protected members.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    import os as _os

    # Same-PGID child (eligible — in workspace, not caller).
    same_pgid_child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=False,
    )
    # Different-PGID child (eligible — in workspace, separate group).
    diff_pgid_child = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(ws),
        start_new_session=True,
    )
    # Outside negative control (in its own session).
    control = subprocess.Popen(
        ["sleep", "300"],
        cwd=str(outside),
        start_new_session=True,
    )
    try:
        time.sleep(0.15)

        # Change cwd into workspace so same-PGID children are detected.
        old_cwd = os.getcwd()
        try:
            os.chdir(str(ws))
            result = kb.close_workspace_processes(ws)
        finally:
            os.chdir(old_cwd)

        # Caller survived.
        assert result["skipped_self"] >= 1
        # Both in-workspace children were signalled (same-PGID by PID,
        # diff-PGID by killpg).
        assert result["signalled"] >= 2
        # Outside process was skipped.
        assert result["skipped_outside"] >= 1
        # No identity mismatches — all real identities match.
        assert result["skipped_identity_mismatch"] == 0

        # In-workspace children are dead.
        same_pgid_child.wait(timeout=5)
        diff_pgid_child.wait(timeout=5)
        assert same_pgid_child.returncode != 0
        assert diff_pgid_child.returncode != 0

        # Outside control survived.
        assert control.poll() is None
    finally:
        for p in [same_pgid_child, diff_pgid_child, control]:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass
