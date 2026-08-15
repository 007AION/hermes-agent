#!/usr/bin/env python3
"""AION controlled PR review/merge gate.

System intent: one narrow executable permanently allowlisted in Hermes instead of
raw gh/bash/python merge/review commands. The tool performs deterministic
preflight checks before any GitHub review or merge side effect.

AION-CORE-PR6 gate-epoch CAS: the merge action now performs an atomic
gate-epoch compare-and-swap against the native Kanban DB before any external
GitHub write.  Stale intent (newer epoch, non-APPROVE decision, head drift)
fails closed before the merge adapter is invoked.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_CONFIGS: dict[str, dict[str, Any]] = {
    "kiddhu/aion-governance": {
        "protected_issues": (879, 882, 883),
        "hard_forbidden_autoclose_issues": (879, 882, 883),
    },
    # CatalogFlow / SeekAPI implementation repo lane. This gate still enforces
    # exact head, role separation, CLEAN merge state, approvals, and green/current
    # checks, but there are no same-repo protected control issues to re-read here;
    # the authoritative control issue for this lane lives in kiddhu/aion-governance.
    "AION-Empire/deepseek-global-wrapper": {
        "protected_issues": (),
        "hard_forbidden_autoclose_issues": (),
    },
    # Authoritative AION fork of hermes-agent — canonical deploy source.
    # Enforces exact head, role separation, CLEAN merge state, approvals, and
    # green/current checks.  The gate-epoch CAS lives in the native Kanban DB.
    "kiddhu/hermes-agent": {
        "protected_issues": (),
        "hard_forbidden_autoclose_issues": (),
    },
}
AUTHZ_TERMS = (
    "authorization",
    "authorized_scope",
    "allowed_scope",
    "forbidden_actions",
    "forbidden actions",
    "monarch_authorization_required",
)
HARD_BOUNDARY_TERMS = (
    "production pass",
    "monarch accept",
    "full unattended-ready pass",
    "real payment",
    "legal commitment",
    "customer data write",
)
ROLE_EXPECTATIONS: dict[str, dict[str, str]] = {
    "bafuxunan": {"actor": "GemAION", "reviewer": "GemAION"},
    "agent007": {"actor": "007AION", "reviewer": "GemAION"},
}


def die(code: str, detail: str, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"ok": False, "code": code, "detail": detail}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    raise SystemExit(2)


def repo_config(repo: str) -> dict[str, Any]:
    cfg = REPO_CONFIGS.get(repo)
    if cfg is None:
        die("REPO_FORBIDDEN", "repository is outside AION controlled gate", {"repo": repo, "allowed_repos": sorted(REPO_CONFIGS)})
    return cfg


def forbidden_autoclose_re(issue_numbers: tuple[int, ...]) -> re.Pattern[str] | None:
    if not issue_numbers:
        return None
    alternation = "|".join(str(n) for n in issue_numbers)
    return re.compile(rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?:{alternation})\b", re.IGNORECASE)


def run_gh(args: list[str], gh_config_dir: str, *, input_text: str | None = None) -> Any:
    env = os.environ.copy()
    env["GH_CONFIG_DIR"] = gh_config_dir
    cp = subprocess.run(
        ["gh", *args],
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if cp.returncode != 0:
        die("GH_FAILED", "gh command failed", {"args": args[:4], "stderr": cp.stderr[-800:]})
    out = cp.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def gh_text(args: list[str], gh_config_dir: str) -> str:
    env = os.environ.copy()
    env["GH_CONFIG_DIR"] = gh_config_dir
    cp = subprocess.run(["gh", *args], text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if cp.returncode != 0:
        die("GH_FAILED", "gh command failed", {"args": args[:4], "stderr": cp.stderr[-800:]})
    return cp.stdout.strip()


def ensure_actor(gh_config_dir: str, actor: str) -> str:
    current = run_gh(["api", "user", "--jq", ".login"], gh_config_dir)
    if current == actor:
        return actor
    die("ACTOR_MISMATCH", "active GitHub actor mismatch; refusing to switch credentials inside controlled gate", {"required": actor, "actual": current})


def ensure_runtime_identity(args: argparse.Namespace, active_actor: str) -> None:
    runtime_role = args.runtime_role or os.environ.get("HERMES_PROFILE") or ""
    if not runtime_role:
        die("RUNTIME_ROLE_MISSING", "runtime role is required for role/github identity binding")
    expected = ROLE_EXPECTATIONS.get(runtime_role)
    if expected is None:
        if runtime_role in {"gm", "gm2"}:
            return
        die("RUNTIME_ROLE_UNKNOWN", "runtime role is not in the controlled role map", {"runtime_role": runtime_role, "known_roles": sorted(ROLE_EXPECTATIONS) + ["gm", "gm2"]})
    if active_actor != expected["actor"]:
        die("RUNTIME_ROLE_ACTOR_MISMATCH", "runtime role is not bound to the expected GitHub actor", {"runtime_role": runtime_role, "expected_actor": expected["actor"], "actual_actor": active_actor})
    if args.actor != expected["actor"]:
        die("REQUESTED_ACTOR_MISMATCH", "requested actor does not match runtime role binding", {"runtime_role": runtime_role, "expected_actor": expected["actor"], "requested_actor": args.actor})
    if args.reviewer != expected["reviewer"]:
        die("EXPECTED_REVIEWER_MISMATCH", "expected reviewer does not match runtime role binding", {"runtime_role": runtime_role, "expected_reviewer": expected["reviewer"], "requested_reviewer": args.reviewer})


def _check_sort_key(check: dict[str, Any]) -> tuple[str, str, str]:
    """Return a conservative recency key for GitHub statusCheckRollup items."""
    return (
        str(check.get("completedAt") or ""),
        str(check.get("startedAt") or ""),
        str(check.get("detailsUrl") or check.get("targetUrl") or ""),
    )


def normalize_checks(rollup: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize GitHub statusCheckRollup to the latest entry per check name.

    GitHub can retain superseded failed CheckRun entries in statusCheckRollup after
    a workflow is re-run for the same exact head. gh pr checks reports only the
    current/latest check per name, so the controlled gate must make the same
    currentness distinction instead of treating stale failures as live blockers.
    If entries for a name lack any recency signal, keep all of them so the gate
    fails closed rather than silently dropping ambiguous status.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in rollup or []:
        name = c.get("name") or c.get("context") or c.get("__typename") or "UNKNOWN_CHECK"
        grouped.setdefault(str(name), []).append(c)

    checks: list[dict[str, Any]] = []
    for name, entries in grouped.items():
        if len(entries) > 1 and all(_check_sort_key(e) == ("", "", "") for e in entries):
            selected_entries = entries
        else:
            selected_entries = [max(entries, key=_check_sort_key)]
        for c in selected_entries:
            checks.append({
                "name": name,
                "status": c.get("status"),
                "conclusion": c.get("conclusion") or c.get("state"),
                "startedAt": c.get("startedAt"),
                "completedAt": c.get("completedAt"),
                "detailsUrl": c.get("detailsUrl") or c.get("targetUrl"),
            })
    return checks


def verify(args: argparse.Namespace, *, require_open: bool, require_approved: bool, require_merger: bool) -> dict[str, Any]:
    cfg = repo_config(args.repo)
    if not re.fullmatch(r"[0-9a-f]{40}", args.head or ""):
        die("BAD_HEAD", "exact head must be a full 40-char sha")
    active_actor = ensure_actor(args.gh_config_dir, args.actor)
    ensure_runtime_identity(args, active_actor)
    pr = run_gh([
        "pr", "view", str(args.pr), "--repo", args.repo,
        "--json", "number,state,url,author,headRefOid,mergeStateStatus,reviewDecision,statusCheckRollup,body,title,baseRefName,headRefName,isDraft",
    ], args.gh_config_dir)
    if pr.get("number") != args.pr:
        die("PR_MISMATCH", "PR number mismatch", {"pr": pr.get("number")})
    state = pr.get("state")
    if require_open and state != "OPEN":
        die("PR_NOT_OPEN", "PR must be OPEN before this action", {"state": state})
    if not require_open and state not in {"OPEN", "MERGED"}:
        die("PR_STATE_FORBIDDEN", "PR must be OPEN or MERGED for preflight/readback", {"state": state})
    if pr.get("author", {}).get("login") != args.author:
        die("AUTHOR_MISMATCH", "PR author mismatch", {"expected": args.author, "actual": pr.get("author", {}).get("login")})
    if pr.get("headRefOid") != args.head:
        die("STALE_HEAD", "live PR head does not equal approved exact head", {"expected": args.head, "actual": pr.get("headRefOid")})
    if pr.get("baseRefName") != args.base:
        die("BASE_MISMATCH", "PR base mismatch", {"expected": args.base, "actual": pr.get("baseRefName")})
    if pr.get("isDraft"):
        die("DRAFT_PR", "draft PR cannot pass gate")
    if require_open and pr.get("mergeStateStatus") != "CLEAN":
        die("MERGE_STATE_NOT_CLEAN", "merge state must be CLEAN", {"actual": pr.get("mergeStateStatus")})
    checks = normalize_checks(pr.get("statusCheckRollup") or [])
    bad_checks = [c for c in checks if c.get("conclusion") not in {"SUCCESS", "SKIPPED"}]
    if bad_checks:
        die("CHECKS_NOT_GREEN", "all checks must be green/current", {"bad_checks": bad_checks})
    if require_approved and pr.get("reviewDecision") != "APPROVED":
        die("NOT_APPROVED", "PR reviewDecision must be APPROVED", {"actual": pr.get("reviewDecision")})
    reviews = run_gh(["pr", "view", str(args.pr), "--repo", args.repo, "--json", "reviews"], args.gh_config_dir).get("reviews") or []
    reviewer_approved = [r for r in reviews if r.get("author", {}).get("login") == args.reviewer and r.get("state") == "APPROVED"]
    if require_approved and not reviewer_approved:
        die("REVIEWER_APPROVAL_MISSING", "required reviewer APPROVED review missing", {"reviewer": args.reviewer})
    body = (pr.get("body") or "")
    lower_body = body.lower()
    if not any(t in lower_body for t in AUTHZ_TERMS):
        die("AUTHORIZATION_ENVELOPE_MISSING", "PR body lacks Authorization Envelope / scope boundary terms")
    hard_forbidden_re = forbidden_autoclose_re(tuple(cfg.get("hard_forbidden_autoclose_issues") or ()))
    if hard_forbidden_re and hard_forbidden_re.search(body):
        die("PROTECTED_ISSUE_AUTOCLOSE", "PR body contains auto-close keyword for protected issue")
    if args.author == args.reviewer:
        die("ROLE_COLLISION", "author and reviewer must differ")
    if require_merger:
        if args.actor in {args.author, args.reviewer}:
            die("MERGER_ROLE_COLLISION", "merger actor must differ from author and reviewer", {"actor": args.actor})
    issues: dict[str, str] = {}
    for n in tuple(cfg.get("protected_issues") or ()):
        issue = run_gh(["issue", "view", str(n), "--repo", args.repo, "--json", "number,state,url"], args.gh_config_dir)
        issues[str(n)] = issue.get("state")
        if issue.get("state") != "OPEN":
            die("PROTECTED_ISSUE_NOT_OPEN", "protected issue must remain OPEN", {"issue": n, "state": issue.get("state")})
    return {
        "repo": args.repo,
        "pr": args.pr,
        "url": pr.get("url"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "author": args.author,
        "reviewer": args.reviewer,
        "runtime_role": args.runtime_role or os.environ.get("HERMES_PROFILE") or "",
        "actor": active_actor,
        "exact_head": args.head,
        "merge_state": pr.get("mergeStateStatus"),
        "review_decision": pr.get("reviewDecision"),
        "checks": checks,
        "protected_issues": issues,
        "authorization_envelope": "PRESENT",
    }


def action_preflight(args: argparse.Namespace) -> None:
    evidence = verify(args, require_open=args.expect_open, require_approved=args.require_approved, require_merger=args.require_merger)
    evidence.update({"ok": True, "action": "preflight", "timestamp": datetime.now(timezone.utc).isoformat()})
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


def action_review(args: argparse.Namespace) -> None:
    evidence = verify(args, require_open=True, require_approved=False, require_merger=False)
    if args.actor != args.reviewer:
        die("REVIEW_ACTOR_MISMATCH", "review action must run as reviewer actor", {"actor": args.actor, "reviewer": args.reviewer})
    body = args.review_body or "AION controlled exact-head review: APPROVED / PASS_WITH_LIMITS."
    if any(term in body.lower() for term in HARD_BOUNDARY_TERMS):
        die("OVERBROAD_REVIEW_BODY", "review body contains overbroad hard-boundary pass/accept term")
    if not args.dry_run:
        gh_text(["pr", "review", str(args.pr), "--repo", args.repo, "--approve", "--body", body], args.gh_config_dir)
    evidence.update({"ok": True, "action": "review", "dry_run": args.dry_run})
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


# ---------------------------------------------------------------------------
# Gate-epoch CAS at the supported merge-write boundary (AION-CORE-PR6)
# ---------------------------------------------------------------------------

def _real_github_merge_adapter(
    repo: str, pr_number: int, head: str, decision: str, stage: str,
    *,
    gh_config_dir: str,
    subject: str,
    merge_body: str,
) -> dict:
    """Real GitHub merge adapter — called ONLY after gate-epoch CAS passes.

    This function is the actual external write: ``gh pr merge --squash``.
    It is passed as the ``merge_adapter`` to :func:`merge_with_gate_cas`,
    which validates CAS first and calls this only on PASS.
    """
    env = os.environ.copy()
    env["GH_CONFIG_DIR"] = gh_config_dir
    cmd = [
        "gh", "pr", "merge", str(pr_number), "--repo", repo, "--squash",
        "--subject", subject or f"AION controlled merge PR #{pr_number}",
        "--body", merge_body or "AION controlled role-separated merge.",
    ]
    cp = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    if cp.returncode != 0:
        # A missing branch cleanup ref can happen after merge landed.
        post = run_gh(["pr", "view", str(pr_number), "--repo", repo, "--json", "state,mergeCommit,mergedBy,headRefOid"], gh_config_dir)
        if post.get("state") != "MERGED":
            return {"merged": False, "error": cp.stderr[-800:] or cp.stdout[-800:], "post_state": post.get("state")}
        return {"merged": True, "warning": cp.stderr[-800:] or cp.stdout[-800:], "post_merge": post}
    post = run_gh(["pr", "view", str(pr_number), "--repo", repo, "--json", "state,mergeCommit,mergedBy,headRefOid"], gh_config_dir)
    return {"merged": True, "post_merge": post}


def action_merge(args: argparse.Namespace, *, merge_adapter: Callable | None = None) -> None:
    """Perform a controlled merge with gate-epoch CAS.

    After the standard :func:`verify` preflight, this function opens the
    native Kanban DB and calls :func:`merge_with_gate_cas` to atomically
    validate the gate state.  A stale intent (newer epoch, non-APPROVE
    decision, head drift) fails closed before any external write.

    In production, the ``merge_adapter`` is ``None`` and CAS operates in
    read-validate mode; the real GitHub merge follows the original code
    path.  In test mode, a fake ``merge_adapter`` is injected — CAS
    calls it only on PASS, and the real GitHub merge path is skipped.
    """
    evidence = verify(args, require_open=True, require_approved=True, require_merger=True)
    if args.method != "squash":
        die("METHOD_FORBIDDEN", "only squash merge is allowed")

    # -----------------------------------------------------------------------
    # Gate-epoch CAS (AION-CORE-PR6): the supported merge-write boundary.
    # This must run BEFORE any external GitHub write.  In test mode a fake
    # adapter is injected; in production CAS validates and the real merge
    # follows the original path below.
    # -----------------------------------------------------------------------
    gate_epoch = getattr(args, "gate_epoch", None)
    kanban_db_path = getattr(args, "kanban_db", None)

    if gate_epoch is not None:
        # Resolve the kanban DB — import is lazy so tests without a
        # HERMES_HOME / kanban DB can still import this module.
        from hermes_cli.kanban_db import merge_with_gate_cas, connect as kanban_connect

        if kanban_db_path:
            os.environ["HERMES_KANBAN_DB"] = kanban_db_path

        with kanban_connect() as conn:
            cas = merge_with_gate_cas(
                conn, args.repo, args.pr,
                intent_epoch=gate_epoch,
                intent_decision="APPROVE",
                intent_head=args.head,
                merge_adapter=merge_adapter,
            )
            if not cas["valid"]:
                die(
                    "GATE_CAS_FAILED",
                    "Gate-epoch CAS rejected merge — stale intent discarded",
                    {
                        "mismatch_reason": cas.get("mismatch_reason"),
                        "gate_id": cas["gate_id"],
                        "current_epoch": cas["current"]["epoch"],
                        "current_decision": cas["current"]["decision"],
                    },
                )
            evidence["gate_cas"] = {
                "gate_id": cas["gate_id"],
                "epoch": cas["current"]["epoch"],
                "decision": cas["current"]["decision"],
                "adapter_called": cas["adapter_called"],
            }

            # If a fake adapter was injected (test mode), skip the real
            # GitHub merge — the adapter was already called by CAS above.
            if merge_adapter is not None:
                evidence.update({
                    "ok": True,
                    "action": "merge",
                    "dry_run": args.dry_run,
                    "adapter_injected": True,
                })
                print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
                return

    # -----------------------------------------------------------------------
    # Production path: real GitHub merge (unchanged from original binary,
    # but now gated behind the CAS check above).
    # -----------------------------------------------------------------------
    if not args.dry_run:
        env = os.environ.copy()
        env["GH_CONFIG_DIR"] = args.gh_config_dir
        cmd = [
            "gh", "pr", "merge", str(args.pr), "--repo", args.repo, "--squash",
            "--subject", args.subject or f"AION controlled merge PR #{args.pr}",
            "--body", args.merge_body or "AION controlled role-separated merge.",
        ]
        cp = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
        if cp.returncode != 0:
            # A missing branch cleanup ref can happen after the merge already landed. Read back before failing.
            post = run_gh(["pr", "view", str(args.pr), "--repo", args.repo, "--json", "state,mergeCommit,mergedBy,headRefOid"], args.gh_config_dir)
            if post.get("state") != "MERGED":
                die("MERGE_FAILED", "controlled merge failed and PR is not merged", {"stderr": cp.stderr[-800:], "post": post})
            evidence["merge_warning"] = cp.stderr[-800:] or cp.stdout[-800:]
        post = run_gh(["pr", "view", str(args.pr), "--repo", args.repo, "--json", "state,mergeCommit,mergedBy,headRefOid"], args.gh_config_dir)
        if post.get("state") != "MERGED":
            die("MERGE_READBACK_FAILED", "post-merge readback did not show MERGED", {"post": post})
        if post.get("headRefOid") != args.head:
            die("POST_MERGE_HEAD_MISMATCH", "post-merge head mismatch", {"post": post})
        if post.get("mergedBy", {}).get("login") != args.actor:
            die("POST_MERGER_MISMATCH", "post-merge merger mismatch", {"post": post})
        evidence["post_merge"] = post
    evidence.update({"ok": True, "action": "merge", "dry_run": args.dry_run})
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AION controlled PR gate")
    sub = p.add_subparsers(dest="action", required=True)
    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo", required=True)
        sp.add_argument("--pr", type=int, required=True)
        sp.add_argument("--head", required=True)
        sp.add_argument("--author", required=True)
        sp.add_argument("--reviewer", default="GemAION")
        sp.add_argument("--actor", required=True)
        sp.add_argument("--runtime-role", default="")
        sp.add_argument("--base", default="main")
        sp.add_argument("--gh-config-dir", default=os.environ.get("GH_CONFIG_DIR") or "/root/.config/gh")
        sp.add_argument("--dry-run", action="store_true")
    pre = sub.add_parser("preflight"); common(pre)
    pre.add_argument("--expect-open", action="store_true")
    pre.add_argument("--require-approved", action="store_true")
    pre.add_argument("--require-merger", action="store_true")
    rev = sub.add_parser("review"); common(rev)
    rev.add_argument("--review-body", default="")
    merge = sub.add_parser("merge"); common(merge)
    merge.add_argument("--method", default="squash")
    merge.add_argument("--subject", default="")
    merge.add_argument("--merge-body", default="")
    # Gate-epoch CAS (AION-CORE-PR6)
    merge.add_argument(
        "--gate-epoch", type=int, default=None,
        help="Expected gate epoch for CAS at the merge-write boundary",
    )
    merge.add_argument(
        "--kanban-db", default=None,
        help="Path to kanban.db (overrides HERMES_KANBAN_DB env var)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.action == "preflight":
        action_preflight(args)
    elif args.action == "review":
        action_review(args)
    elif args.action == "merge":
        action_merge(args)
    else:
        die("UNKNOWN_ACTION", "unknown action")


if __name__ == "__main__":
    main()
