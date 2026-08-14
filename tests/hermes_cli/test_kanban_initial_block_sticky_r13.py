"""Creation-time ``initial_status='blocked'`` intent must be durable (R13).

Regression tests for AION-RL2-CORE-01-R13 (canonical incident t_e690dcc1).

Defect: ``create_task(initial_status="blocked")`` parks a real external gate
(e.g. a financial/legal/property-loss action) directly in ``blocked`` to avoid
the running→blocked race, but it records only a ``created`` event whose payload
``status`` is ``"blocked"`` — no ``blocked`` event.  ``_has_sticky_block``
recognised only ``blocked``/``unblocked`` events, so ``recompute_ready`` saw the
task as auto-recoverable and promoted it to ``ready``, after which the
dispatcher claimed/spawned it *before* any authorised unblock.  The same class
previously bit live tasks t_0f0f15da and t_29f75728.

These tests are hermetic: ``HERMES_KANBAN_DB`` is pinned to a fresh temp file,
``Path.home`` is redirected, and ``HERMES_HOME`` is a temp dir, so no code path
can read or write the operator's live board.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Hermetic Kanban DB bound to a temp file via ``HERMES_KANBAN_DB``.

    Proves zero live-board writes/signals: the DB path is a fresh temp file,
    ``Path.home`` is redirected, and ``HERMES_HOME`` is a temp dir — so the
    resolution chain can never fall back to ``~/.hermes/kanban.db``.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    db_file = tmp_path / "isolated" / "kanban.db"
    db_file.parent.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_file))
    kb.init_db()
    return db_file


# ---------------------------------------------------------------------------
# RED — creation-time blocked intent must survive recompute / dispatch
# ---------------------------------------------------------------------------

def test_create_blocked_no_parents_stays_blocked_across_recompute(isolated_db):
    """A task created ``initial_status='blocked'`` with no parents must stay
    blocked across repeated ``recompute_ready`` cycles — never auto-promoted."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="gated", assignee="a",
                           initial_status="blocked")
        assert kb.get_task(conn, t).status == "blocked"

        for _ in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert kb.get_task(conn, t).status == "blocked"


def test_create_blocked_no_parents_not_dispatched(isolated_db):
    """The dispatcher must not claim/spawn a creation-blocked task.

    ``dispatch_once`` internally runs ``recompute_ready`` then claims every
    ``ready`` task; a creation-blocked task must never become claimable.
    """
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="gated", assignee="a",
                           initial_status="blocked")
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert spawns == []
        assert kb.get_task(conn, t).status == "blocked"
        # Direct claim must also fail: only 'ready' is claimable.
        assert kb.claim_task(conn, t) is None


def test_create_blocked_with_terminal_parent_stays_blocked(isolated_db):
    """A creation-blocked task whose parent is already done must still stay
    blocked (the parent gate is satisfied, but the explicit block is not)."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="done")

        child = kb.create_task(conn, title="gated", assignee="a",
                               parents=[parent], initial_status="blocked")
        assert kb.get_task(conn, child).status == "blocked"

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"


def test_create_blocked_with_nonterminal_parent_stays_blocked(isolated_db):
    """A creation-blocked task with an open parent stays blocked (both the
    explicit block AND the open parent keep it out of the work pool)."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="gated", assignee="a",
                               parents=[parent], initial_status="blocked")
        assert kb.get_task(conn, child).status == "blocked"

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, child).status == "blocked"


# ---------------------------------------------------------------------------
# GREEN — explicit unblock is the only exit
# ---------------------------------------------------------------------------

def test_explicit_unblock_no_parents_goes_ready(isolated_db):
    """Explicit unblock of a parent-free creation-blocked task yields ready."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="gated", assignee="a",
                           initial_status="blocked")
        assert kb.unblock_task(conn, t) is True
        assert kb.get_task(conn, t).status == "ready"
        # And it is now claimable by the dispatcher.
        assert kb.claim_task(conn, t) is not None


def test_explicit_unblock_open_parent_goes_todo(isolated_db):
    """Explicit unblock with an open parent yields todo (parent-aware)."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="gated", assignee="a",
                               parents=[parent], initial_status="blocked")
        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "todo"

        # Completing the parent then lets normal recompute promote to ready
        # (complete_task internally runs recompute_ready).
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="done")
        assert kb.get_task(conn, child).status == "ready"


def test_explicit_unblock_all_parents_done_goes_ready(isolated_db):
    """Explicit unblock with all parents already done yields ready."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="done")

        child = kb.create_task(conn, title="gated", assignee="a",
                               parents=[parent], initial_status="blocked")
        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# GREEN — archived is a legal terminal parent state for explicit unblock
# (AION-RL2-CORE-01-R13 audit repair: unblock_task must treat done AND
#  archived parents as terminal, not just done)
# ---------------------------------------------------------------------------

def test_explicit_unblock_archived_parent_goes_ready(isolated_db):
    """Explicit unblock with an ``archived`` parent must land immediately
    ``ready`` — archived is a legal terminal parent state, same as done."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        kb.archive_task(conn, parent)
        assert kb.get_task(conn, parent).status == "archived"

        child = kb.create_task(conn, title="gated", assignee="a",
                               parents=[parent], initial_status="blocked")
        assert kb.get_task(conn, child).status == "blocked"

        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "ready"
        # And it is now claimable by the dispatcher.
        assert kb.claim_task(conn, child) is not None


def test_explicit_unblock_mixed_done_and_archived_parents_goes_ready(isolated_db):
    """All parents terminal (a mix of done and archived) → explicit unblock
    yields ready, not todo."""
    with kb.connect() as conn:
        p_done = kb.create_task(conn, title="p_done", assignee="a")
        kb.claim_task(conn, p_done)
        kb.complete_task(conn, p_done, result="done")

        p_arch = kb.create_task(conn, title="p_arch", assignee="a")
        kb.archive_task(conn, p_arch)

        child = kb.create_task(conn, title="gated", assignee="a",
                               parents=[p_done, p_arch],
                               initial_status="blocked")
        assert kb.get_task(conn, child).status == "blocked"

        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "ready"


def test_explicit_unblock_open_and_archived_parents_goes_todo(isolated_db):
    """Any nonterminal parent keeps the child in ``todo`` on unblock, even
    when the other parent is already archived."""
    with kb.connect() as conn:
        p_open = kb.create_task(conn, title="p_open", assignee="a")
        p_arch = kb.create_task(conn, title="p_arch", assignee="a")
        kb.archive_task(conn, p_arch)

        child = kb.create_task(conn, title="gated", assignee="a",
                               parents=[p_open, p_arch],
                               initial_status="blocked")
        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "todo"

        # Completing the open parent then lets recompute promote to ready.
        kb.claim_task(conn, p_open)
        kb.complete_task(conn, p_open, result="done")
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# GREEN — legacy rows (created event records status=blocked, no blocked event)
# ---------------------------------------------------------------------------

def test_legacy_created_blocked_row_stays_sticky(isolated_db):
    """A legacy creation-time blocked row (created payload status=blocked with
    no later blocked/unblocked event) must remain sticky across recompute."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="legacy-gate", assignee="a")
        # Rewrite to the exact pre-fix shape: the task was parked blocked at
        # creation, so the created event recorded status=blocked and no
        # separate 'blocked' event ever fired.
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.execute(
            "UPDATE task_events SET payload=? "
            "WHERE task_id=? AND kind='created'",
            (json.dumps({"status": "blocked"}), t),
        )
        conn.commit()

        assert kb._has_sticky_block(conn, t) is True
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"


def test_legacy_created_blocked_unblock_not_falsely_restuck(isolated_db):
    """After an explicit unblock, a legacy creation-blocked row must not be
    falsely re-stuck on a later recompute."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="legacy-gate", assignee="a")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.execute(
            "UPDATE task_events SET payload=? "
            "WHERE task_id=? AND kind='created'",
            (json.dumps({"status": "blocked"}), t),
        )
        conn.commit()

        assert kb.unblock_task(conn, t) is True
        assert kb._has_sticky_block(conn, t) is False
        assert kb.get_task(conn, t).status == "ready"

        # A subsequent recompute must NOT re-block it.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "ready"


# ---------------------------------------------------------------------------
# Regression — dependency / circuit-breaker blocks must not be conflated
# ---------------------------------------------------------------------------

def test_dependency_block_without_block_event_still_auto_recovers(isolated_db):
    """A task manually parked blocked (no block event, created status !=
    blocked) is a dependency/circuit-style block and must still auto-recover."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="a")
        child = kb.create_task(conn, title="child", assignee="a",
                               parents=[parent])
        kb.claim_task(conn, parent)
        kb.complete_task(conn, parent, result="done")
        # Manually block the child (simulates a dependency block, no explicit
        # kanban_block call → no 'blocked' event).
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=0, "
            "last_failure_error=NULL WHERE id=?",
            (child,),
        )
        conn.commit()
        assert kb.get_task(conn, child).status == "blocked"

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert kb.get_task(conn, child).status == "ready"


def test_circuit_breaker_block_still_respects_failure_limit(isolated_db):
    """A circuit-breaker trip (``gave_up``, created status != blocked) is not an
    explicit gate and must still honour the failure-limit guard, not be treated
    as a creation-time sticky block."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="flaky", assignee="a")
        kb.claim_task(conn, t)
        kb._record_task_failure(conn, t, error="boom", outcome="timed_out",
                                release_claim=True, end_run=True,
                                failure_limit=2)
        # First failure is below the limit → auto-recovered to ready.
        assert kb.get_task(conn, t).status == "ready"

        # Trip the breaker to blocked (still NOT a creation-time block).
        kb.claim_task(conn, t)
        kb._record_task_failure(conn, t, error="boom", outcome="timed_out",
                                release_claim=True, end_run=True,
                                failure_limit=2)
        assert kb.get_task(conn, t).status == "blocked"
        # Not sticky: created status was 'ready', no explicit block event.
        assert kb._has_sticky_block(conn, t) is False
        # recompute respects the failure-limit guard and does not promote.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"


# ---------------------------------------------------------------------------
# GREEN — manual promote_task must not bypass explicit block intent
# ---------------------------------------------------------------------------

def test_promote_creation_blocked_refused_force_false(isolated_db):
    """``promote_task(force=False)`` must refuse a creation-time-blocked task
    and the task must remain unclaimable."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="gated", assignee="a",
                           initial_status="blocked")
        ok, err = kb.promote_task(conn, t, actor="tester")
        assert ok is False
        assert err is not None and "explicit gate" in err
        assert kb.get_task(conn, t).status == "blocked"
        # Still not claimable: only 'ready' is claimable.
        assert kb.claim_task(conn, t) is None


def test_promote_creation_blocked_refused_force_true(isolated_db):
    """``force=True`` overrides the *parent* gate, not an explicit
    human/external gate — it must also refuse a creation-time block."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="gated", assignee="a",
                           initial_status="blocked")
        ok, err = kb.promote_task(conn, t, actor="tester", force=True)
        assert ok is False
        assert err is not None and "explicit gate" in err
        assert kb.get_task(conn, t).status == "blocked"
        assert kb.claim_task(conn, t) is None


def test_promote_non_sticky_blocked_still_works(isolated_db):
    """A blocked task with no sticky signal (dependency/circuit-style direct
    block) must remain manually promotable — the sticky guard must not
    over-block legitimate manual recovery."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="t", assignee="a")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.commit()
        ok, err = kb.promote_task(conn, t, actor="tester", reason="recover")
        assert ok is True and err is None
        assert kb.get_task(conn, t).status == "ready"


# ---------------------------------------------------------------------------
# GREEN — fail closed on unreadable creation-origin evidence
# ---------------------------------------------------------------------------

def test_malformed_created_payload_fails_closed(isolated_db):
    """A ``created`` event whose payload is malformed JSON must be treated as
    an unreadable origin and stay sticky (fail closed), never auto-promoted."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="corrupt-gate", assignee="a")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.execute(
            "UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
            ("{not valid json", t),
        )
        conn.commit()
        assert kb._has_sticky_block(conn, t) is True
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"


def test_non_dict_created_payload_fails_closed(isolated_db):
    """A ``created`` payload that is valid JSON but not a dict must fail
    closed (sticky)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="corrupt-gate", assignee="a")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.execute(
            "UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
            ('[1, 2, 3]', t),
        )
        conn.commit()
        assert kb._has_sticky_block(conn, t) is True
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"


def test_non_string_created_status_fails_closed(isolated_db):
    """A ``created`` payload whose ``status`` is present but not a string is
    ambiguous and must fail closed (sticky)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="corrupt-gate", assignee="a")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.execute(
            "UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
            (json.dumps({"status": None}), t),
        )
        conn.commit()
        assert kb._has_sticky_block(conn, t) is True
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"


def test_created_without_status_key_not_sticky(isolated_db):
    """A legacy decompose-created event (valid dict, no ``status`` key) is a
    normal creation, not a gate — it must NOT be treated as sticky so
    dependency/circuit-breaker recovery for decomposed children is preserved."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="decomposed-child", assignee="a")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
        conn.execute(
            "UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
            (json.dumps({"by": "decomposer", "from_decompose_of": "root"}), t),
        )
        conn.commit()
        assert kb._has_sticky_block(conn, t) is False
        assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, t).status == "ready"


# ---------------------------------------------------------------------------
# GREEN — created is immutable origin; a later duplicate must not re-stick
# ---------------------------------------------------------------------------

def test_adversarial_duplicate_created_does_not_restick_after_unblock(isolated_db):
    """After an explicit unblock, a later duplicate ``created {status: blocked}``
    event must not re-stick the task — the ``unblocked`` transition is
    authoritative and the duplicate origin is ignored."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="gated", assignee="a",
                           initial_status="blocked")
        assert kb.unblock_task(conn, t) is True
        assert kb.get_task(conn, t).status == "ready"

        # Adversarial: re-insert a duplicate 'created' event with block intent
        # AFTER the unblock (it gets a higher event id).
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'created', ?, ?)",
            (t, json.dumps({"status": "blocked"}), int(time.time())),
        )
        conn.commit()

        assert kb._has_sticky_block(conn, t) is False
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "ready"


def test_explicit_block_event_then_unblock_not_restuck(isolated_db):
    """A creation-time block that also receives an explicit ``blocked`` event
    (worker/operator) and then an explicit unblock must end un-stuck — the
    most recent transition wins over both the origin and the block event."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="gated", assignee="a",
                           initial_status="blocked")
        # Simulate an explicit re-block after a (hypothetical) claim, then
        # unblock: the unblocked transition must be authoritative.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (t, json.dumps({"reason": "review-required"}), int(time.time())),
        )
        conn.commit()
        assert kb._has_sticky_block(conn, t) is True

        assert kb.unblock_task(conn, t) is True
        assert kb._has_sticky_block(conn, t) is False
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "ready"
