"""Write-lock budget: transcript appends must survive a competing writer.

Regression for the gm2 Factory Director incident (AION-SHARED-INFRA-
GM2-STATE-DB-LOCK-FD-CRON-R1): with a 3GB+ state.db and a fragmented FTS5
index, another process's full FTS5 ``optimize`` (automerge) held the WAL write
lock for minutes (measured ~24.5s unicode61 + ~140.4s trigram = ~164.8s at 250K
messages / 78K+225K FTS blocks). The previous ``_execute_write`` retried a
FIXED 15 attempts (~15s of total wait), so a cron transcript append exhausted
its budget and failed ``database is locked`` -> ``session_persistence_failed``
-> the turn's transcript was lost.

The repair has two parts:

1. The automatic write-path FTS maintenance is now BOUNDED (``merge=N`` via
   :meth:`SessionDB.merge_fts`, sub-second) instead of the full ``optimize``
   (minutes), so no long writer starves a transcript append.
2. The retry is bounded by WALL-CLOCK time (``_WRITE_LOCK_TIMEOUT_S``) so an
   append waits out a legitimate competing writer, while still failing closed
   with an explicit ``sqlite3.OperationalError`` when the budget is exhausted.
"""

import sqlite3
import threading
import time

import pytest

import hermes_state
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Deterministic deadline-logic tests (monkeypatched clock — no real sleeps)
# ──────────────────────────────────────────────────────────────────────────

def _freeze_clock(monkeypatch):
    """Monkeypatch hermes_state.time/random so the retry loop is deterministic.

    ``monotonic`` returns a fake clock; ``sleep`` advances it; ``uniform``
    returns the midpoint so jitter is a fixed 85ms.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(hermes_state.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        hermes_state.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)
    )
    monkeypatch.setattr(
        hermes_state.random, "uniform", lambda a, b: (a + b) / 2.0
    )
    return clock


class TestWriteLockBudgetLogic:
    def test_retries_within_budget_then_succeeds(self, db, monkeypatch):
        """A transient lock is retried (jitter) until the write succeeds."""
        _freeze_clock(monkeypatch)
        db._WRITE_LOCK_TIMEOUT_S = 5.0
        calls = {"n": 0}

        def _flaky(conn):
            calls["n"] += 1
            if calls["n"] < 4:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert db._execute_write(_flaky) == "ok"
        # 3 lock failures + 1 success.
        assert calls["n"] == 4

    def test_fails_closed_when_budget_exhausted(self, db, monkeypatch):
        """When the lock never clears within the budget, the write fails with
        an explicit ``database is locked`` error — bounded, not an infinite
        loop, and the caller observes a hard failure (no silent drop)."""
        clock = _freeze_clock(monkeypatch)
        db._WRITE_LOCK_TIMEOUT_S = 1.0
        attempts = {"n": 0}

        def _always_locked(conn):
            attempts["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError) as ei:
            db._execute_write(_always_locked)
        assert "locked" in str(ei.value).lower()
        # Bounded: the loop stopped at the deadline, not on a fixed count of 15.
        # Each iteration burns 85ms of jitter, so ~11 iterations fit in 1.0s.
        assert attempts["n"] >= 1
        assert clock["t"] >= 1000.0 + 1.0  # the deadline was actually reached

    def test_zero_budget_fails_immediately(self, db, monkeypatch):
        """A zero budget must fail on the first lock error (fail-fast)."""
        _freeze_clock(monkeypatch)
        db._WRITE_LOCK_TIMEOUT_S = 0.0
        with pytest.raises(sqlite3.OperationalError):
            db._execute_write(lambda conn: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")))

    def test_non_lock_error_still_propagates_immediately(self, db, monkeypatch):
        """Only lock/busy errors are retried; everything else propagates."""
        _freeze_clock(monkeypatch)
        db._WRITE_LOCK_TIMEOUT_S = 5.0
        with pytest.raises(sqlite3.IntegrityError):
            db._execute_write(
                lambda conn: (_ for _ in ()).throw(
                    sqlite3.IntegrityError("NOT NULL constraint failed"))
            )


# ──────────────────────────────────────────────────────────────────────────
# Real multi-connection contention (the multi-process-equivalent case)
# ──────────────────────────────────────────────────────────────────────────

class TestWriteLockContention:
    def test_append_survives_real_competing_writer_atomically(self, tmp_path):
        """A transcript append waits out a competing writer that holds the WAL
        write lock, then persists exactly one row (no loss, no duplicate)."""
        d = SessionDB(db_path=tmp_path / "state.db")
        try:
            d.create_session("s1", source="test")
            # Short busy timeout so the append exercises the retry loop rather
            # than blocking the full 1s on a single BEGIN IMMEDIATE.
            d._conn.execute("PRAGMA busy_timeout=40")
            d._WRITE_LOCK_TIMEOUT_S = 5.0

            raw = sqlite3.connect(
                str(tmp_path / "state.db"), timeout=0.05, check_same_thread=False
            )
            raw.execute("BEGIN IMMEDIATE")  # hold the write lock

            def _hold_then_release():
                time.sleep(0.4)  # long enough to force several retries
                raw.commit()
                raw.close()

            t = threading.Thread(target=_hold_then_release)
            t.start()
            msg_id = d.append_message("s1", "user", "survives contention")
            t.join(timeout=5)

            msgs = d.get_messages("s1")
            assert [m["content"] for m in msgs] == ["survives contention"]
            assert msg_id is not None
        finally:
            d.close()

    def test_append_fails_closed_when_lock_outlasts_budget(self, tmp_path):
        """When the competing writer holds the lock past the budget, the
        append raises ``database is locked`` and does NOT persist a partial
        row — the caller sees an explicit failure, never silent loss."""
        d = SessionDB(db_path=tmp_path / "state.db")
        try:
            d.create_session("s1", source="test")
            d._conn.execute("PRAGMA busy_timeout=40")
            d._WRITE_LOCK_TIMEOUT_S = 0.2  # budget too small to wait it out

            raw = sqlite3.connect(
                str(tmp_path / "state.db"), timeout=0.05, check_same_thread=False
            )
            raw.execute("BEGIN IMMEDIATE")  # hold the write lock

            def _hold_long():
                time.sleep(1.0)  # far longer than the 0.2s budget
                raw.commit()
                raw.close()

            t = threading.Thread(target=_hold_long)
            t.start()
            with pytest.raises(sqlite3.OperationalError) as ei:
                d.append_message("s1", "user", "must not be silently dropped")
            assert "locked" in str(ei.value).lower()
            t.join(timeout=5)

            # Fail-closed: no partial/duplicate row was written.
            assert d.get_messages("s1") == []
        finally:
            d.close()

    def test_contention_never_duplicates_or_drops_messages(self, tmp_path):
        """Concurrent appends from two independent SessionDB connections (the
        gateway + cron shape) all land exactly once under contention."""
        db_path = tmp_path / "state.db"
        a = SessionDB(db_path=db_path)
        b = SessionDB(db_path=db_path)
        try:
            a.create_session("s1", source="test")
            a._conn.execute("PRAGMA busy_timeout=40")
            b._conn.execute("PRAGMA busy_timeout=40")
            a._WRITE_LOCK_TIMEOUT_S = 5.0
            b._WRITE_LOCK_TIMEOUT_S = 5.0

            results = {"a": [], "b": []}
            errs = []

            def writer(sess, out, label):
                try:
                    for i in range(20):
                        out.append(
                            sess.append_message("s1", "user", f"{label}-msg-{i}")
                        )
                except Exception as e:  # pragma: no cover - unexpected
                    errs.append(e)

            ta = threading.Thread(target=writer, args=(a, results["a"], "a"))
            tb = threading.Thread(target=writer, args=(b, results["b"], "b"))
            ta.start()
            tb.start()
            ta.join(timeout=20)
            tb.join(timeout=20)

            assert not errs
            msgs = a.get_messages("s1")
            # 40 distinct messages, none dropped, none duplicated.
            assert len(msgs) == 40
            assert len({m["content"] for m in msgs}) == 40
        finally:
            a.close()
            b.close()


class TestFtsMaintenanceWriterContention:
    """The ACTUAL causal writer is the FTS5 merge/optimize maintenance, not a
    bare ``BEGIN IMMEDIATE``. These tests contend a transcript append against
    the real FTS5 maintenance writer (the bounded ``merge_fts`` the cadence
    now runs), proving the write path no longer starves appends the way the
    old full ``optimize`` did."""

    def test_append_survives_competing_real_merge_fts(self, tmp_path):
        """A transcript append contends with the real FTS5 merge writer and
        still lands exactly once (no drop, no duplicate)."""
        db_path = tmp_path / "state.db"
        d = SessionDB(db_path=db_path)
        try:
            d.create_session("s1", source="test")
            for i in range(200):
                d.append_message("s1", "user", f"seed needle token {i} alpha bravo")
            d._conn.execute("PRAGMA busy_timeout=40")
            d._WRITE_LOCK_TIMEOUT_S = 5.0

            # Competing writer = the actual FTS5 bounded merge (the cadence's
            # writer), driven from a second SessionDB on the same file.
            other = SessionDB(db_path=db_path)
            other._conn.execute("PRAGMA busy_timeout=40")
            other._WRITE_LOCK_TIMEOUT_S = 5.0

            def _merge_writer():
                for _ in range(10):
                    other.merge_fts(16)

            t = threading.Thread(target=_merge_writer)
            t.start()
            msg_id = d.append_message("s1", "user", "survives real fts merge")
            t.join(timeout=10)

            assert msg_id is not None
            contents = [m["content"] for m in d.get_messages("s1")]
            assert contents.count("survives real fts merge") == 1
        finally:
            d.close()

    def test_bounded_merge_does_not_block_append_long(self, tmp_path):
        """The bounded merge writer (merge_fts) returns in sub-second time and
        never holds the write lock long enough to exhaust the retry budget."""
        db_path = tmp_path / "state.db"
        d = SessionDB(db_path=db_path)
        try:
            d.create_session("s1", source="test")
            for i in range(200):
                d.append_message("s1", "user", f"seed needle token {i} alpha bravo")
            started = time.monotonic()
            n = d.merge_fts(16)
            elapsed = time.monotonic() - started
            assert n == 2
            # Bounded: far below the 30s budget even on a fragmented index.
            assert elapsed < 5.0, elapsed
        finally:
            d.close()
