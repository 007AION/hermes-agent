"""Deterministic tests for the AION controlled PR gate protected-issue config.

Regression context (CatalogFlow supersession, 2026-08-15): the controlled
gate in ``scripts/aion_controlled_pr_gate.py`` still required CatalogFlow
control issues #673/#691/#682 to be OPEN, but those three issues were
intentionally closed/not_planned by the owner under the #879 supersession.
The mandatory merge preflight therefore failed closed with
``PROTECTED_ISSUE_NOT_OPEN issue=673 state=CLOSED`` even though the current
control issues #879/#882/#883 are all OPEN and the PR under gate is APPROVED,
CLEAN, and green.

These tests pin the repaired contract:

1. ``kiddhu/aion-governance`` protects the current controls (879, 882, 883)
   and no longer requires the superseded (673, 691, 682) to be OPEN.
2. A mocked preflight with #879/#882/#883 OPEN passes the protected-issue
   stage while every other hard invariant is also satisfied.
3. Any one of #879/#882/#883 being CLOSED fails closed with
   ``PROTECTED_ISSUE_NOT_OPEN``.
4. Auto-close keywords (close/fix/resolve and their inflections) targeting
   #879/#882/#883 are rejected with ``PROTECTED_ISSUE_AUTOCLOSE``.

No live GitHub call is made: ``run_gh`` is mocked and the merge/kanban path
is never entered.
"""

import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import aion_controlled_pr_gate as gate  # noqa: E402

HEAD = "0" * 40  # valid 40-char hex head

CURRENT_CONTROLS = (879, 882, 883)
SUPERSEDED_CONTROLS = (673, 691, 682)


def make_args(**overrides):
    a = types.SimpleNamespace(
        repo="kiddhu/aion-governance",
        pr=884,
        head=HEAD,
        author="kiddhu",
        reviewer="GemAION",
        actor="007AION",
        runtime_role="gm2",
        base="main",
        gh_config_dir="/tmp/gh",
        dry_run=False,
        expect_open=False,
        require_approved=True,
        require_merger=True,
        method="squash",
        subject="",
        merge_body="",
        gate_epoch=None,
        kanban_db=None,
    )
    for k, v in overrides.items():
        setattr(a, k, v)
    return a


def make_run_gh(issue_states, body="authorization envelope present", pr_state="OPEN"):
    """Return a deterministic ``run_gh`` stand-in.

    ``issue_states`` maps issue number -> GitHub state string. Only the issue
    numbers actually queried by the gate config matter; anything else defaults
    to OPEN.
    """

    def fake_run_gh(args, gh_config_dir, *, input_text=None):
        if args[0] == "api" and "user" in args:
            return "007AION"  # active actor login
        if args[0] == "pr" and args[1] == "view":
            json_spec = args[-1]
            if "reviews" in json_spec:
                return {"reviews": [{"author": {"login": "GemAION"}, "state": "APPROVED"}]}
            return {
                "number": 884,
                "state": pr_state,
                "url": "https://github.com/kiddhu/aion-governance/pull/884",
                "author": {"login": "kiddhu"},
                "headRefOid": HEAD,
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "APPROVED",
                "statusCheckRollup": [],
                "body": body,
                "title": "test PR",
                "baseRefName": "main",
                "headRefName": "test-branch",
                "isDraft": False,
            }
        if args[0] == "issue" and args[1] == "view":
            n = int(args[2])
            state = issue_states.get(n, "OPEN")
            return {"number": n, "state": state, "url": f"https://github.com/kiddhu/aion-governance/issues/{n}"}
        raise AssertionError(f"unexpected run_gh args: {args}")

    return fake_run_gh


# ── config contract ─────────────────────────────────────────────────────


def test_repo_config_protects_current_catalogflow_controls():
    cfg = gate.REPO_CONFIGS["kiddhu/aion-governance"]
    assert cfg["protected_issues"] == CURRENT_CONTROLS
    assert cfg["hard_forbidden_autoclose_issues"] == CURRENT_CONTROLS
    for superseded in SUPERSEDED_CONTROLS:
        assert superseded not in cfg["protected_issues"]
        assert superseded not in cfg["hard_forbidden_autoclose_issues"]


def test_other_repo_configs_unchanged():
    assert gate.REPO_CONFIGS["AION-Empire/deepseek-global-wrapper"]["protected_issues"] == ()
    assert gate.REPO_CONFIGS["AION-Empire/deepseek-global-wrapper"]["hard_forbidden_autoclose_issues"] == ()
    assert gate.REPO_CONFIGS["kiddhu/hermes-agent"]["protected_issues"] == ()
    assert gate.REPO_CONFIGS["kiddhu/hermes-agent"]["hard_forbidden_autoclose_issues"] == ()


# ── positive mocked preflight ───────────────────────────────────────────


def test_positive_preflight_passes_with_current_controls_open():
    fake = make_run_gh({879: "OPEN", 882: "OPEN", 883: "OPEN"})
    with patch.object(gate, "run_gh", side_effect=fake):
        evidence = gate.verify(make_args(), require_open=True, require_approved=True, require_merger=True)
    assert evidence["protected_issues"] == {"879": "OPEN", "882": "OPEN", "883": "OPEN"}
    # superseded controls are no longer read by the gate at all
    assert set(evidence["protected_issues"]) == {"879", "882", "883"}
    assert evidence["exact_head"] == HEAD
    assert evidence["merge_state"] == "CLEAN"
    assert evidence["review_decision"] == "APPROVED"


# ── negative: each current control CLOSED fails closed ──────────────────


@pytest.mark.parametrize("closed_issue", [879, 882, 883])
def test_negative_preflight_fails_closed_when_any_current_control_closed(closed_issue, capsys):
    states = {879: "OPEN", 882: "OPEN", 883: "OPEN"}
    states[closed_issue] = "CLOSED"
    fake = make_run_gh(states)
    with patch.object(gate, "run_gh", side_effect=fake):
        with pytest.raises(SystemExit) as exc:
            gate.verify(make_args(), require_open=True, require_approved=True, require_merger=True)
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "PROTECTED_ISSUE_NOT_OPEN" in out
    assert f"issue\": {closed_issue}" in out or f"\"issue\": {closed_issue}" in out


# ── auto-close keywords targeting current controls rejected ─────────────


@pytest.mark.parametrize("issue", [879, 882, 883])
@pytest.mark.parametrize(
    "keyword",
    ["close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved"],
)
def test_autoclose_keyword_targeting_current_control_rejected(keyword, issue, capsys):
    body = f"authorization envelope: this PR will {keyword} #{issue} as part of the supersession"
    fake = make_run_gh({879: "OPEN", 882: "OPEN", 883: "OPEN"}, body=body)
    with patch.object(gate, "run_gh", side_effect=fake):
        with pytest.raises(SystemExit) as exc:
            gate.verify(make_args(), require_open=True, require_approved=True, require_merger=True)
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "PROTECTED_ISSUE_AUTOCLOSE" in out
