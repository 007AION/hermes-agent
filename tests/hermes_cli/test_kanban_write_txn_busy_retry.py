"""write_txn BUSY-retry behaviour.

These tests target the transaction boundary (BEGIN IMMEDIATE / COMMIT) only.
On unmodified main write_txn has no application-level retry, so the
"transient BUSY is absorbed" and "persistent BUSY is bounded" cases fail until
the fix lands. No real DB is touched: a fake connection records and replays
scripted boundary outcomes.
"""

import sqlite3

import pytest

from hermes_cli import kanban_db as kb


class _FakeConn:
    """Records execute() calls and replays a scripted result per SQL statement.

    script maps an uppercased SQL prefix to a list of outcomes consumed in
    order. An outcome is either an Exception (raised) or None (success).

    ``in_transaction`` mirrors a real sqlite3.Connection so write_txn's
    ambiguous-COMMIT reconciliation can consult it: BEGIN IMMEDIATE opens the
    transaction, a successful COMMIT/ROLLBACK closes it, and a COMMIT that
    raises leaves it open (the commit did not land).
    """

    def __init__(self, script):
        self._script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self.in_transaction = False

    def execute(self, sql, *args):
        self.calls.append(sql)
        key = sql.strip().split()[0].upper()
        outcomes = self._script.get(key)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        if key == "BEGIN":
            self.in_transaction = True
        elif key in ("COMMIT", "ROLLBACK"):
            self.in_transaction = False
        return None

    def count(self, prefix):
        prefix = prefix.upper()
        return sum(1 for c in self.calls if c.strip().upper().startswith(prefix))


def _busy():
    return sqlite3.OperationalError("database is locked")


def _other():
    return sqlite3.OperationalError("no such table: tasks")


@pytest.fixture(autouse=True)
def _no_file_check(monkeypatch):
    # Isolate the boundary behaviour from the post-commit invariant.
    monkeypatch.setattr(kb, "_check_file_length_invariant", lambda conn: None)


def test_retry_sleep_respects_floor(monkeypatch):
    # The jitter has a floor so a retry can't busy-spin back into the collision.
    slept = []
    monkeypatch.setattr(kb.time, "sleep", lambda s: slept.append(s))
    conn = _FakeConn({"BEGIN": [_busy(), _busy(), None]})
    with kb.write_txn(conn):
        pass
    assert slept
    assert all(s >= kb._BUSY_RETRY_MIN_S for s in slept)
    assert all(s <= kb._BUSY_RETRY_MAX_S for s in slept)


def test_transient_busy_at_begin_is_absorbed():
    conn = _FakeConn({"BEGIN": [_busy(), None]})
    with kb.write_txn(conn):
        pass
    assert conn.count("BEGIN") == 2
    assert conn.count("COMMIT") == 1


def test_transient_busy_at_commit_is_absorbed():
    conn = _FakeConn({"COMMIT": [_busy(), None]})
    with kb.write_txn(conn):
        pass
    assert conn.count("COMMIT") == 2


def test_non_busy_operational_error_is_not_retried():
    conn = _FakeConn({"BEGIN": [_other()]})
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with kb.write_txn(conn):
            pass
    assert conn.count("BEGIN") == 1


def test_persistent_busy_is_bounded_and_reraises():
    conn = _FakeConn({"BEGIN": [_busy()] * 50})
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        with kb.write_txn(conn):
            pass
    # Bounded: a finite number of attempts, not 50.
    assert conn.count("BEGIN") < 50


def test_body_is_not_replayed_on_commit_retry():
    conn = _FakeConn({"COMMIT": [_busy(), None]})
    body_runs = 0
    with kb.write_txn(conn):
        body_runs += 1
    assert body_runs == 1


def test_clean_path_commits_once():
    conn = _FakeConn({})
    with kb.write_txn(conn):
        pass
    assert conn.count("BEGIN") == 1


def test_persistent_busy_at_commit_rolls_back():
    # Exhausted COMMIT leaves the txn open; write_txn must ROLLBACK before
    # re-raising so the connection isn't poisoned for the next transaction.
    conn = _FakeConn({"COMMIT": [_busy()] * 50})
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        with kb.write_txn(conn):
            pass
    assert conn.count("ROLLBACK") == 1


def test_missing_in_transaction_capability_is_fail_safe_not_landed():
    # A connection double/proxy that lacks ``in_transaction`` (an older or
    # third-party contract) must not raise AttributeError and must not be
    # silently classified as landed: write_txn treats the missing capability as
    # "still in transaction", so the exhausted COMMIT still rolls back and
    # re-raises the original sqlite3.OperationalError.
    conn = _FakeConn({"COMMIT": [_busy()] * 50})
    del conn.in_transaction
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        with kb.write_txn(conn):
            pass
    assert conn.count("ROLLBACK") == 1


class _OpaqueTxnConn:
    """Connection double that HIDES ``in_transaction`` (R6 opaque boundary).

    Unlike ``_FakeConn`` it never exposes ``in_transaction`` (accessing it
    raises AttributeError), so ``write_txn`` must fall back to the authoritative
    ROLLBACK probe. It models a single underlying transaction:

    * BEGIN opens the transaction.
    * COMMIT either lands (closes the transaction) then raises, or raises
      without landing (transaction stays open), per ``commit_lands``.
    * ROLLBACK raises "no transaction is active" when the transaction is already
      closed (landed), otherwise closes it and succeeds.
    """

    def __init__(self, commit_raises=None, *, commit_lands=False):
        self.commit_raises = commit_raises
        self.commit_lands = commit_lands
        self.calls = []
        self._open = False

    def execute(self, sql, *args):
        key = sql.strip().split()[0].upper()
        self.calls.append(key)
        if key == "BEGIN":
            self._open = True
        elif key == "COMMIT":
            if self.commit_lands:
                self._open = False
            if self.commit_raises is not None:
                raise self.commit_raises
            self._open = False
        elif key == "ROLLBACK":
            if not self._open:
                raise sqlite3.OperationalError("cannot rollback - no transaction is active")
            self._open = False
        return None

    def count(self, prefix):
        prefix = prefix.upper()
        return sum(1 for c in self.calls if c.upper().startswith(prefix))


def test_opaque_landed_commit_is_reconciled_as_landed():
    # An opaque connection double that HIDES ``in_transaction`` but durably
    # landed the COMMIT before raising must be reconciled as LANDED: write_txn
    # must NOT re-raise (the commit is complete) and must NOT roll back the
    # already-closed transaction. The authoritative signal is the ROLLBACK
    # probe raising "no transaction is active".
    err = sqlite3.OperationalError("injected: COMMIT landed then error")
    conn = _OpaqueTxnConn(commit_raises=err, commit_lands=True)
    with kb.write_txn(conn):  # must NOT raise
        pass
    assert conn.count("ROLLBACK") == 1  # the authoritative probe, not a rollback


def test_opaque_nonlanded_commit_reraises_original():
    # An opaque connection double that HIDES ``in_transaction`` and whose COMMIT
    # did NOT land must be reconciled as NOT-landed: write_txn rolls back and
    # re-raises the ORIGINAL sqlite3.OperationalError (never AttributeError).
    err = sqlite3.OperationalError("injected: COMMIT did not land")
    conn = _OpaqueTxnConn(commit_raises=err, commit_lands=False)
    with pytest.raises(sqlite3.OperationalError, match="did not land"):
        with kb.write_txn(conn):
            pass
    assert conn.count("ROLLBACK") == 1
