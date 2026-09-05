"""Hostile/idempotency tests for the GM directive -> Native binding prefix.

Covers the AION-GM-DIRECTIVE-NATIVE-CONTINUITY-R1 repair: the three durable
pre-claim events (``directive_observed`` -> ``directive_selected`` ->
``directive_bound_native``) recorded on the existing ``task_events`` surface,
plus their fail-closed binding invariants.
"""

from __future__ import annotations

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


def _mk_task(conn, **kw):
    return kb.create_task(conn, title="gm directive carrier", assignee="gm", **kw)


def _bind(conn, task_id, *, source_sha="sha-001", source_ref="#833 comment 5548300539",
          observer="gm2", selector="gm2", assignee="gm2", **kw):
    return kb.record_directive_intake(
        conn,
        task_id=task_id,
        source_ref=source_ref,
        source_sha_or_immutable_id=source_sha,
        observer_profile=observer,
        selector_profile=selector,
        assignee=assignee,
        **kw,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_records_observed_selected_bound_in_order(kanban_home):
    conn = kb.connect()
    try:
        t = _mk_task(conn)
        r = _bind(conn, t, source_ref="#833 comment 5548300539", source_sha="sha-001")
        assert r["already_bound"] is False

        lineage = kb.directive_binding_lineage(conn, t)
        assert [e.kind for e in lineage] == [
            kb.DIRECTIVE_OBSERVED_KIND,
            kb.DIRECTIVE_SELECTED_KIND,
            kb.DIRECTIVE_BOUND_NATIVE_KIND,
        ]

        observed, selected, bound = lineage
        assert observed.payload["source_ref"] == "#833 comment 5548300539"
        assert observed.payload["source_sha_or_immutable_id"] == "sha-001"
        assert observed.payload["observer_profile"] == "gm2"
        assert observed.payload["observed_at"] is not None

        assert selected.payload["selector_profile"] == "gm2"
        assert selected.payload["disposition"] == kb.DIRECTIVE_EXECUTE_DISPOSITION

        assert bound.payload["task_id"] == t
        assert bound.payload["assignee"] == "gm2"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotency: duplicate patrol read -> one logical lineage, no duplicate events
# ---------------------------------------------------------------------------

def test_duplicate_read_is_idempotent(kanban_home):
    conn = kb.connect()
    try:
        t = _mk_task(conn, idempotency_key="directive-001")
        _bind(conn, t, source_sha="sha-dup")
        r2 = _bind(conn, t, source_sha="sha-dup")
        assert r2["already_bound"] is True
        assert r2["events"] == []

        lineage = kb.directive_binding_lineage(conn, t)
        # Still exactly one observed/selected/bound triple.
        assert [e.kind for e in lineage] == [
            kb.DIRECTIVE_OBSERVED_KIND,
            kb.DIRECTIVE_SELECTED_KIND,
            kb.DIRECTIVE_BOUND_NATIVE_KIND,
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fail-closed: one directive -> two tasks
# ---------------------------------------------------------------------------

def test_one_directive_cannot_bind_two_tasks(kanban_home):
    conn = kb.connect()
    try:
        t1 = _mk_task(conn)
        t2 = _mk_task(conn)
        _bind(conn, t1, source_sha="sha-two-tasks")
        with pytest.raises(RuntimeError):
            _bind(conn, t2, source_sha="sha-two-tasks")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Non-authoritative observer cannot become executable
# ---------------------------------------------------------------------------

def test_non_gm_observer_fails_closed(kanban_home):
    conn = kb.connect()
    try:
        t = _mk_task(conn)
        with pytest.raises(PermissionError):
            _bind(conn, t, observer="agent007")
        with pytest.raises(PermissionError):
            _bind(conn, t, selector="bafuxunan")
        # Nothing durable was written.
        assert kb.directive_binding_lineage(conn, t) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prose-only / missing immutable id fails closed
# ---------------------------------------------------------------------------

def test_missing_source_ref_or_sha_fails_closed(kanban_home):
    conn = kb.connect()
    try:
        t = _mk_task(conn)
        with pytest.raises(ValueError):
            kb.record_directive_intake(
                conn, task_id=t, source_ref="", source_sha_or_immutable_id="sha-x",
                observer_profile="gm", selector_profile="gm", assignee="gm",
            )
        with pytest.raises(ValueError):
            kb.record_directive_intake(
                conn, task_id=t, source_ref="#ref", source_sha_or_immutable_id="  ",
                observer_profile="gm", selector_profile="gm", assignee="gm",
            )
    finally:
        conn.close()


def test_awareness_is_not_selection(kanban_home):
    conn = kb.connect()
    try:
        t = _mk_task(conn)
        with pytest.raises(ValueError):
            _bind(conn, t, disposition="OBSERVED")
        assert kb.directive_binding_lineage(conn, t) == []
    finally:
        conn.close()


def test_bind_to_unknown_task_fails_closed(kanban_home):
    conn = kb.connect()
    try:
        with pytest.raises(ValueError):
            _bind(conn, "t_does_not_exist")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Observed-but-not-selected cannot claim a task by implication
# ---------------------------------------------------------------------------

def test_events_do_not_claim_task(kanban_home):
    conn = kb.connect()
    try:
        t = _mk_task(conn)
        _bind(conn, t)
        # Recording the prefix events must not claim/dispatch/run the task:
        # status stays ready, no current_run_id, no worker pid, no claimed run.
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.current_run_id is None
        runs = conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ?", (t,),
        ).fetchall()
        assert runs == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stale/superseded directive does not silently overwrite current binding
# ---------------------------------------------------------------------------

def test_superseded_directive_does_not_overwrite(kanban_home):
    conn = kb.connect()
    try:
        t1 = _mk_task(conn)
        _bind(conn, t1, source_sha="sha-v1")
        # A new (superseding) directive has a different immutable id -> a new,
        # separate lineage on a different task; the v1 binding is preserved.
        t2 = _mk_task(conn)
        _bind(conn, t2, source_sha="sha-v2")

        l1 = kb.directive_binding_lineage(conn, t1)
        l2 = kb.directive_binding_lineage(conn, t2)
        assert l1[0].payload["source_sha_or_immutable_id"] == "sha-v1"
        assert l2[0].payload["source_sha_or_immutable_id"] == "sha-v2"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Restart/replay: lifecycle reconstructable from persisted events
# ---------------------------------------------------------------------------

def test_lineage_survives_reconnect(kanban_home):
    conn = kb.connect()
    try:
        t = _mk_task(conn)
        _bind(conn, t, source_sha="sha-replay")
    finally:
        conn.close()

    # Fresh connection (simulating restart): the lineage is fully reconstructable.
    conn2 = kb.connect()
    try:
        lineage = kb.directive_binding_lineage(conn2, t)
        assert [e.kind for e in lineage] == [
            kb.DIRECTIVE_OBSERVED_KIND,
            kb.DIRECTIVE_SELECTED_KIND,
            kb.DIRECTIVE_BOUND_NATIVE_KIND,
        ]
        assert lineage[0].payload["source_sha_or_immutable_id"] == "sha-replay"
    finally:
        conn2.close()
