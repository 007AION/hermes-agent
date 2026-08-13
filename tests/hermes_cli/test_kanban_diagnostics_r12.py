"""Isolated RED→GREEN tests for AION-RL2-CORE-01-R12 board diagnostics repair.

The installed ``compute_board_diagnostics`` undercounted open obligations
because its static ``_NONTERMINAL_STATUSES`` omitted ``fenced`` (a live
nonterminal lifecycle), and it defined ``executable_now`` as dispatchable
``ready`` only — so ``executable_now=0`` was confused with "no unfinished
work" even when active/reclaimable running work was in flight.

These tests prove the repaired contract:

* ``done`` / ``archived`` are the ONLY terminal exclusions; every other
  current or future status is fail-closed as an open obligation.
* ``executable_now`` = dispatchable ready + active running + safely
  reclaimable running, never fenced wait states, with no double counting.
* detached/stale ``task_runs`` rows never count as active work when the
  authoritative ``tasks.status`` is terminal.

File: tests/hermes_cli/test_kanban_diagnostics_r12.py
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim_running(conn, title, *, pid=None, expires=None):
    """Create + claim a task, then stamp worker_pid / claim_expires directly."""
    t = kb.create_task(conn, title=title, assignee="a")
    kb.claim_task(conn, t)
    if pid is not None:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, t))
    if expires is not None:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?", (expires, t)
        )
    conn.commit()
    return t


def _make_fenced(conn, assignee="a"):
    """Park a task in ``fenced`` (nonterminal wait state, never executable)."""
    t = kb.create_task(conn, title="fenced-wait", assignee=assignee)
    conn.execute(
        "UPDATE tasks SET status = 'fenced', worker_pid = 424242, "
        "worker_starttime = 111222 WHERE id = ?",
        (t,),
    )
    conn.commit()
    return t


# ── RED / GREEN: open obligations are fail-closed ────────────────────────────


def test_r12_open_obligations_counts_fenced_and_blocked(kanban_home):
    """blocked=1 + fenced=1 yields open_obligations=2 (not the old 1)."""
    with kb.connect() as conn:
        blocked = kb.create_task(conn, title="blocked", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (blocked,)
        )
        _make_fenced(conn)
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["status_counts"].get("blocked") == 1
    assert diag["status_counts"].get("fenced") == 1
    assert diag["open_obligations"] == 2


def test_r12_fenced_included_in_nonterminal_detail(kanban_home):
    """The executable_zero finding lists fenced in nonterminal detail."""
    with kb.connect() as conn:
        blocked = kb.create_task(conn, title="blocked", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ?", (blocked,)
        )
        _make_fenced(conn)
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 0
    finding = diag["findings"][0]
    assert finding["kind"] == "executable_zero_open_obligations"
    nonterminal = finding["data"]["nonterminal_states"]
    assert nonterminal.get("blocked") == 1
    assert nonterminal.get("fenced") == 1
    assert "fenced" in finding["data"]["open_statuses"]


def test_r12_done_and_archived_are_only_terminal_exclusions(kanban_home):
    """done and archived are excluded from obligations; fenced is not."""
    with kb.connect() as conn:
        done = kb.create_task(conn, title="done", assignee="a")
        kb.claim_task(conn, done)
        kb.complete_task(conn, done, result="ok")
        arch = kb.create_task(conn, title="archived", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?", (arch,)
        )
        _make_fenced(conn)
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["open_obligations"] == 1  # only the fenced task
    assert diag["status_counts"].get("done") == 1
    assert "archived" not in diag["status_counts"]  # excluded from counts


def test_r12_unknown_future_status_is_fail_closed(kanban_home):
    """An unknown nonterminal status must count as an open obligation."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="future-status", assignee="a")
        conn.execute("UPDATE tasks SET status = 'zombie' WHERE id = ?", (t,))
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["status_counts"].get("zombie") == 1
    assert diag["open_obligations"] == 1


# ── executable_now decomposition ─────────────────────────────────────────────


def test_r12_executable_now_includes_active_running(kanban_home, monkeypatch):
    """Active running work counts toward executable_now (not just ready)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    with kb.connect() as conn:
        _claim_running(conn, "active-running", pid=424243)
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 1
    assert diag["executable_components"]["active_running"] == 1
    assert diag["executable_components"]["reclaimable_running"] == 0
    assert diag["executable_components"]["dispatchable_ready"] == 0
    assert diag["open_obligations"] == 1


def test_r12_executable_now_includes_reclaimable_running(kanban_home, monkeypatch):
    """Safely reclaimable running work counts toward executable_now."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect() as conn:
        _claim_running(
            conn, "reclaimable-running", pid=424244,
            expires=int(time.time()) - 60,
        )
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 1
    assert diag["executable_components"]["reclaimable_running"] == 1
    assert diag["executable_components"]["active_running"] == 0


def test_r12_executable_now_excludes_fenced_and_guarded_ready(
    kanban_home, monkeypatch,
):
    """Fenced wait states and guarded ready are NOT executable."""
    pr_url = "https://github.com/totemx-AI/subsidysmart/pull/42"
    monkeypatch.setattr(
        kb, "_resolve_github_pr_state", lambda _url: "OPEN", raising=False,
    )
    with kb.connect() as conn:
        _make_fenced(conn)
        current = kb.create_task(conn, title="guarded-ready", assignee="alice")
        owner = kb.create_task(conn, title="active-owner", assignee="bob")
        kb.add_comment(conn, current, "alice", f"Continue {pr_url}")
        kb.add_comment(conn, owner, "bob", f"Working {pr_url}")
        kb.claim_task(conn, owner)
        diag = kd.compute_board_diagnostics(conn)

    # owner (fresh claim) is active running → executable_now == 1.
    assert diag["executable_now"] == 1
    comp = diag["executable_components"]
    assert comp["fenced_waiting"] == 1
    assert comp["guarded_ready"] == 1
    assert comp["dispatchable_ready"] == 0
    assert diag["guarded_ready"] == [
        {"task_id": current, "reason": "active_pr"}
    ]
    # fenced + guarded-ready + owner are all open obligations.
    assert diag["open_obligations"] == 3


def test_r12_no_double_counting_across_all_states(kanban_home, monkeypatch):
    """Each state lands in exactly one bucket; totals are exact."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    with kb.connect() as conn:
        # dispatchable ready
        kb.create_task(conn, title="ready-a", assignee="a")
        # active running (live worker, fresh claim)
        _claim_running(conn, "active-running", pid=424243)
        # reclaimable running (expired claim, no worker pid)
        _claim_running(
            conn, "reclaimable-running", expires=int(time.time()) - 60,
        )
        # fenced wait
        _make_fenced(conn)
        # done
        done = kb.create_task(conn, title="done", assignee="a")
        kb.claim_task(conn, done)
        kb.complete_task(conn, done, result="ok")
        # archived
        arch = kb.create_task(conn, title="archived", assignee="a")
        conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ?", (arch,)
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    comp = diag["executable_components"]
    assert comp["dispatchable_ready"] == 1
    assert comp["active_running"] == 1
    assert comp["reclaimable_running"] == 1
    assert comp["guarded_ready"] == 0
    assert comp["fenced_waiting"] == 1

    assert diag["executable_now"] == 3  # ready + active + reclaimable
    # open obligations: ready(1) + running(2) + fenced(1) = 4
    assert diag["open_obligations"] == 4
    # no double counting: components sum to executable_now
    assert (
        comp["dispatchable_ready"]
        + comp["active_running"]
        + comp["reclaimable_running"]
    ) == diag["executable_now"]


# ── detached / stale run rows must never count as active work ────────────────


def test_r12_stale_run_row_never_counts_as_active_work(kanban_home, monkeypatch):
    """A done task with an orphaned running run row is NOT active work."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="product", assignee="a")
        kb.claim_task(conn, t)                # run 1 (running)
        kb.complete_task(conn, t, result="done")  # run 1 → completed, task done
        # Inject an orphaned stale run row (running / ended_at NULL) that is
        # NOT the task's current_run_id — the historical leak from the live
        # board (t_8e8e8d62 row 664).
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "worker_pid, claim_lock) VALUES (?, ?, 'running', ?, ?, ?)",
            (t, "a", int(time.time()) - 3600, 999999, "host:dead"),
        )
        conn.commit()
        diag = kd.compute_board_diagnostics(conn)

    assert diag["executable_now"] == 0
    assert diag["open_obligations"] == 0
    assert diag["executable_components"]["active_running"] == 0
    assert diag["executable_components"]["reclaimable_running"] == 0
