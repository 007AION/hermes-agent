"""Exact-run repair for open task_runs rows whose task row was deleted.

The regression reproduces the verified R34/R35 residue shape in an isolated DB:
a synthetic worker test created a run, then a raw task-only DELETE removed the
parent task while leaving task_runs/task_events history. The repair is strictly
one-run, identity-bound, fail-closed on ownership, idempotent, and does not scan
or delete unrelated history.
"""

from __future__ import annotations

import builtins
import contextlib
import json
import socket
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.isolated_kanban_env(home):
        kb.init_db()
        yield home


def _missing_task_run(conn, *, profile="author", owned=False):
    task_id = kb.create_task(conn, title="synthetic orphan", assignee=profile)
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = int(claimed.current_run_id)
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    claim_lock = str(run["claim_lock"])
    if not owned:
        # Canonical local claim identity whose PID is provably absent. This
        # models the verified historical stale locks without relying on a live
        # pytest process as their owner.
        claim_lock = f"{socket.gethostname()}:99999999"
        conn.execute(
            "UPDATE task_runs SET claim_lock = ?, claim_expires = ?, "
            "worker_pid = NULL, last_heartbeat_at = NULL WHERE id = ?",
            (claim_lock, int(time.time()) - 60, run_id),
        )
    # Reproduce the verified task-only cleanup. Events and the open run remain.
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    return task_id, run_id, claim_lock


def _repair(conn, task_id, run_id, profile, claim_lock):
    return kb.repair_missing_task_orphan_run(
        conn,
        run_id,
        expected_task_id=task_id,
        expected_profile=profile,
        expected_claim_lock=claim_lock,
    )


def test_missing_task_exact_run_repair_and_deterministic_idempotency(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, claim_lock = _missing_task_run(conn)
        before_events = conn.execute(
            "SELECT id, kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()

        first = _repair(conn, task_id, run_id, "author", claim_lock)
        # Idempotency is bound to the durable exact-run receipt, not to future
        # task-row state. A later task restoration must not preempt replay.
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
            "VALUES (?, 'later restore', 'author', 'ready', 0, ?)",
            (task_id, int(time.time())),
        )
        conn.commit()
        second = _repair(conn, task_id, run_id, "author", claim_lock)
        row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        after_events = conn.execute(
            "SELECT id, kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()

    assert first == second
    assert first["refused"] is None
    assert first["repaired"] is True
    assert first["task_id"] == task_id
    assert first["run_id"] == run_id
    assert first["profile"] == "author"
    assert first["claim_lock"] == claim_lock
    assert first["outcome"] == "reclaimed"
    assert len(first["receipt_sha256"]) == 64
    assert row["ended_at"] is not None
    assert row["status"] == row["outcome"] == "reclaimed"
    assert row["claim_lock"] is None
    assert row["claim_expires"] is None
    assert row["worker_pid"] is None
    assert len(after_events) == len(before_events) + 1
    assert after_events[-1]["kind"] == "missing_task_orphan_run_repaired"


def test_missing_task_repair_refuses_nonmatching_identity_without_mutation(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, claim_lock = _missing_task_run(conn)
        before = dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())
        receipts = [
            kb.repair_missing_task_orphan_run(
                conn, run_id, expected_task_id="t_wrong",
                expected_profile="author", expected_claim_lock=claim_lock,
            ),
            kb.repair_missing_task_orphan_run(
                conn, run_id, expected_task_id=task_id,
                expected_profile="auditor", expected_claim_lock=claim_lock,
            ),
            kb.repair_missing_task_orphan_run(
                conn, run_id, expected_task_id=task_id,
                expected_profile="author", expected_claim_lock="wrong-lock",
            ),
        ]
        after = dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())
    assert [r["refused"] for r in receipts] == [
        "task_identity_mismatch", "profile_identity_mismatch", "claim_identity_mismatch"
    ]
    assert after == before


def test_missing_task_repair_refuses_live_or_ambiguous_ownership(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, claim_lock = _missing_task_run(conn, owned=True)
        before = dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())
        receipt = _repair(conn, task_id, run_id, "author", claim_lock)
        after = dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())
    assert receipt["refused"] == "ambiguous_live_ownership"
    assert receipt["repaired"] is False
    assert after == before


def test_windows_liveness_fallback_never_signals_target(monkeypatch):
    """Fail closed rather than calling Windows ``os.kill(pid, 0)``.

    CPython maps signal 0 to CTRL_C_EVENT on Windows, so the seemingly harmless
    probe may interrupt a live process group. Simulate an install where psutil
    cannot import and assert that the repair helper returns ambiguous/live
    without touching os.kill.
    """
    real_import = builtins.__import__

    def import_without_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated stripped install")
        return real_import(name, *args, **kwargs)

    def forbidden_kill(*_args, **_kwargs):
        raise AssertionError("Windows liveness check must never call os.kill")

    monkeypatch.setattr(builtins, "__import__", import_without_psutil)
    monkeypatch.setattr(kb.os, "name", "nt")
    monkeypatch.setattr(kb.os, "kill", forbidden_kill)

    assert kb._claim_lock_process_is_live(
        f"{socket.gethostname()}:99999999"
    ) is True


def test_missing_task_repair_refuses_present_task_and_distinct_run_untouched(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, claim_lock = _missing_task_run(conn)
        other = kb.create_task(conn, title="distinct live", assignee="auditor")
        other_claim = kb.claim_task(conn, other)
        assert other_claim is not None and other_claim.current_run_id is not None
        other_run_id = int(other_claim.current_run_id)
        other_before = dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (other_run_id,)).fetchone())
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at) "
            "VALUES (?, 'restored', 'author', 'ready', 0, ?)",
            (task_id, int(time.time())),
        )
        conn.commit()
        receipt = _repair(conn, task_id, run_id, "author", claim_lock)
        other_after = dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (other_run_id,)).fetchone())
    assert receipt["refused"] == "task_present"
    assert other_after == other_before


def test_missing_task_absence_check_is_inside_write_transaction(
    kanban_home, monkeypatch,
):
    """A task restored at transaction entry must fence the repair completely.

    This deterministically reproduces the review-found check/write race: on the
    unfenced implementation the initial absence check passed, the injected task
    appeared immediately before the inner write transaction, and the run was
    still reclaimed. The outer IMMEDIATE transaction makes task presence part
    of the same serialized decision and returns ``task_present`` with no run
    mutation.
    """
    with kb.connect() as conn:
        task_id, run_id, claim_lock = _missing_task_run(conn)
        before = dict(
            conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        )
        real_write_txn = kb.write_txn
        injected = False

        @contextlib.contextmanager
        def restore_at_transaction_entry(target_conn):
            nonlocal injected
            with real_write_txn(target_conn):
                if not injected:
                    injected = True
                    target_conn.execute(
                        "INSERT INTO tasks "
                        "(id, title, assignee, status, priority, created_at) "
                        "VALUES (?, 'concurrent restore', 'author', 'ready', 0, ?)",
                        (task_id, int(time.time())),
                    )
                yield

        monkeypatch.setattr(kb, "write_txn", restore_at_transaction_entry)
        receipt = _repair(conn, task_id, run_id, "author", claim_lock)
        after = dict(
            conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        )

    assert injected is True
    assert receipt["refused"] == "task_present"
    assert receipt["repaired"] is False
    assert after == before


def test_missing_task_repair_rolls_back_when_event_append_fails(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, claim_lock = _missing_task_run(conn)
        before = dict(
            conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        )
        conn.execute(
            "CREATE TRIGGER fail_missing_task_repair_event "
            "BEFORE INSERT ON task_events "
            "WHEN NEW.kind = 'missing_task_orphan_run_repaired' "
            "BEGIN SELECT RAISE(ABORT, 'fault injection'); END"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="fault injection"):
            _repair(conn, task_id, run_id, "author", claim_lock)

        after = dict(
            conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        )
        repair_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'missing_task_orphan_run_repaired'",
            (task_id,),
        ).fetchone()[0]

    assert after == before
    assert repair_events == 0


def test_missing_task_exact_run_cli_receipt(kanban_home):
    with kb.connect() as conn:
        task_id, run_id, claim_lock = _missing_task_run(conn)
    out = kc.run_slash(
        "reconcile-missing-task-run "
        f"{run_id} {task_id} --expected-profile author "
        f"--expected-claim-lock {claim_lock}"
    )
    receipt = json.loads(out.splitlines()[0])
    assert receipt["refused"] is None
    assert receipt["repaired"] is True
    assert receipt["run_id"] == run_id
