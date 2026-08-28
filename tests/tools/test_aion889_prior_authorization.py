"""AION-889 exact prior-authorization bridge (no command execution)."""

import copy
import json
import sqlite3
import threading
from io import BytesIO
from unittest.mock import patch

import pytest

import tools.approval as approval
import hermes_constants
from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _isolated_approval_state(monkeypatch):
    approval._session_approved.clear()
    approval._permanent_approved.clear()
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    yield
    approval._session_approved.clear()
    approval._permanent_approved.clear()


def _valid_evidence():
    return {
        "actor": "kiddhu",
        "author_association": "OWNER",
        "comment_id": 5449539635,
        "comment_url": approval._AION889_COMMENT_URL,
        "comment_sha256": approval._AION889_COMMENT_SHA256,
        "comment_created_at": "2026-08-28T07:08:20Z",
        "comment_updated_at": "2026-08-28T07:08:20Z",
        "task_id": "t_420d4177",
        "source_run_id": 3426,
        "pending_command_id": 51021,
        "source_session_id": "20260828_142515_6ae96a",
        "tool_call_id": "call_u2ScsC6Eht5uCoVtg34ubX01",
        "command": approval._AION889_COMMAND,
        "command_sha256": approval._AION889_COMMAND_SHA256,
        "target_profile": "gm",
        "runtime": copy.deepcopy(approval._AION889_RUNTIME),
        "approval_state": "pending_approval",
        "consumed": False,
    }


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("actor", "agent007"),
        ("author_association", "CONTRIBUTOR"),
        ("comment_id", 5449539636),
        ("comment_url", "https://github.com/kiddhu/aion-governance/issues/889"),
        ("comment_sha256", "0" * 64),
        ("comment_updated_at", "2026-08-28T07:08:21Z"),
        ("task_id", "t_other"),
        ("source_run_id", 3427),
        ("pending_command_id", 51022),
        ("source_session_id", "unrelated"),
        ("tool_call_id", "call_other"),
        ("command", "hermes --profile agent007 gateway restart --system"),
        ("command_sha256", "f" * 64),
        ("target_profile", "agent007"),
        ("approval_state", "approved"),
        ("consumed", True),
    ],
)
def test_one_field_drift_fails_closed(field, wrong):
    evidence = _valid_evidence()
    evidence[field] = wrong
    assert approval._aion889_evidence_matches(evidence) is False


@pytest.mark.parametrize("field", ["MainPID", "ExecMainStartTimestamp", "NRestarts", "service"])
def test_runtime_generation_drift_fails_closed(field):
    evidence = _valid_evidence()
    evidence["runtime"][field] = "changed"
    assert approval._aion889_evidence_matches(evidence) is False


def test_exact_evidence_matches():
    assert approval._aion889_evidence_matches(_valid_evidence()) is True


def test_comment_readback_uses_existing_authenticated_github_substrate(monkeypatch):
    payload = {
        "user": {"login": "kiddhu"},
        "author_association": "OWNER",
        "id": approval._AION889_COMMENT_ID,
        "html_url": approval._AION889_COMMENT_URL,
        "body": "frozen body",
        "created_at": "2026-08-28T07:08:20Z",
        "updated_at": "2026-08-28T07:08:20Z",
    }
    captured = {}

    class Auth:
        def get_headers(self):
            return {
                "Accept": "application/vnd.github.v3+json",
                "Authorization": "token secret-never-returned",
            }

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr("tools.skills_hub.GitHubAuth", Auth)
    monkeypatch.setattr(approval.urllib.request, "urlopen", urlopen)

    evidence = approval._read_aion889_comment_evidence()

    assert captured["headers"]["Authorization"] == "token secret-never-returned"
    assert captured["timeout"] == 5
    assert evidence["actor"] == "kiddhu"
    assert evidence["comment_sha256"] == approval.hashlib.sha256(b"frozen body").hexdigest()


def test_comment_readback_refuses_anonymous_github_fallback(monkeypatch):
    class AnonymousAuth:
        def get_headers(self):
            return {"Accept": "application/vnd.github.v3+json"}

    monkeypatch.setattr("tools.skills_hub.GitHubAuth", AnonymousAuth)
    with patch.object(approval.urllib.request, "urlopen") as urlopen:
        with pytest.raises(RuntimeError, match="authenticated GitHub readback unavailable"):
            approval._read_aion889_comment_evidence()
    urlopen.assert_not_called()


@patch("tools.tirith_security.check_command_security", return_value={"action": "allow"})
def test_exact_binding_consumes_once_without_prompt_or_execution(_tirith, monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    consumed = []

    def consume(command):
        consumed.append(command)
        return len(consumed) == 1

    with patch.object(approval, "_try_consume_aion889_prior_authorization", side_effect=consume):
        first = approval.check_all_command_guards(
            approval._AION889_COMMAND,
            "local",
            approval_callback=lambda *args, **kwargs: "deny",
        )
        second = approval.check_all_command_guards(
            approval._AION889_COMMAND,
            "local",
            approval_callback=lambda *args, **kwargs: "deny",
        )

    assert first["approved"] is True
    assert first["prior_authorization_consumed"] is True
    assert second["approved"] is False
    assert second["status"] == "pending_approval"
    assert consumed == [approval._AION889_COMMAND, approval._AION889_COMMAND]


@patch("tools.tirith_security.check_command_security", return_value={
    "action": "warn",
    "findings": [{"rule_id": "extra-risk"}],
    "summary": "additional risky action class",
})
def test_additional_risk_never_uses_prior_binding(_tirith, monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    with patch.object(approval, "_try_consume_aion889_prior_authorization") as consume:
        result = approval.check_all_command_guards(
            approval._AION889_COMMAND,
            "local",
            approval_callback=lambda *args, **kwargs: "deny",
        )
    assert result["approved"] is False
    assert result["status"] == "pending_approval"
    consume.assert_not_called()


def test_changed_command_never_reaches_binding(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_420d4177")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "3427")
    # Byte drift that preserves the same detected action/target class.
    changed = approval._AION889_COMMAND + " "
    with patch.object(approval, "_read_aion889_comment_evidence") as read_comment:
        assert approval._try_consume_aion889_prior_authorization(changed) is False
    read_comment.assert_not_called()


def test_source_session_readback_binds_exact_tool_call_and_pending_result(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, "
            "tool_call_id TEXT, tool_calls TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO messages VALUES (40495, ?, 'assistant', NULL, ?, NULL)",
            (
                approval._AION889_SOURCE_SESSION,
                json.dumps([{
                    "id": approval._AION889_TOOL_CALL_ID,
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": approval._AION889_COMMAND}),
                    },
                }]),
            ),
        )
        conn.execute(
            "INSERT INTO messages VALUES (40496, ?, 'tool', ?, NULL, ?)",
            (
                approval._AION889_SOURCE_SESSION,
                approval._AION889_TOOL_CALL_ID,
                json.dumps({
                    "status": "pending_approval",
                    "command": approval._AION889_COMMAND,
                }),
            ),
        )
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    assert approval._read_aion889_source_evidence() == {
        "source_session_id": approval._AION889_SOURCE_SESSION,
        "tool_call_id": approval._AION889_TOOL_CALL_ID,
        "command": approval._AION889_COMMAND,
        "command_sha256": approval._AION889_COMMAND_SHA256,
        "approval_state": "pending_approval",
    }


def _nonce_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER,
                factory_build_gate INTEGER, factory_terminal_receipt_sha256 TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
                outcome TEXT, ended_at INTEGER
            );
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
                kind TEXT, payload TEXT, created_at INTEGER
            );
            INSERT INTO tasks VALUES ('t_420d4177', 'running', 3427, 1, 'source');
            INSERT INTO task_runs VALUES (3426, 't_420d4177', 'gm', 'blocked', 'blocked', 1);
            INSERT INTO task_runs VALUES (3427, 't_420d4177', 'gm', 'running', NULL, NULL);
            INSERT INTO tasks VALUES ('t_e898d47a', 'done', NULL, 1, 'receipt-sha');
            INSERT INTO task_links VALUES ('t_e898d47a', 't_420d4177');
            """
        )


def test_concurrent_duplicate_consume_is_single_use(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    _nonce_db(db_path)

    def connect():
        conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(kb, "connect", connect)
    barrier = threading.Barrier(2)
    results = []

    def consume():
        barrier.wait()
        results.append(approval._consume_aion889_native_nonce(3427))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    with connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE kind = ?",
            (approval._AION889_EVENT_KIND,),
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["consume_nonce"] == approval._AION889_CONSUME_NONCE


def test_failed_nonce_validation_has_zero_partial_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    _nonce_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET factory_terminal_receipt_sha256 = NULL "
            "WHERE id = 't_e898d47a'"
        )

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(kb, "connect", connect)
    assert approval._consume_aion889_native_nonce(3427) is False
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0