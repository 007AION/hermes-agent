# AION-RL2-CORE-01-R17 — Dir-Workspace Spawn Identity + Activation Evidence Contract

**directive_id:** `AION-RL2-CORE-01-R17-DIR-WORKSPACE-SPAWN-IDENTITY-AND-ACTIVATION-EVIDENCE-REPAIR`
**revision:** `R1` (activation-manifest repair — REQUEST_CHANGES finding F1 from audit `t_2ba77c00`)
**source:** AION-GM2 / 13爷 / Factory Director (`from_gm: gm2`)
**assigned_agent:** agent007 / 007
**formal_record:** https://github.com/kiddhu/aion-governance/issues/790
**audit_target:** bafuxunan (八府巡按) exact-head audit before merge

> 中文摘要：本文件是 R17 修复的审计就绪激活证据契约（修订 R1）。它把「活动可编辑导入源」与「已安装覆盖层」分开绑定，列出四个 R15/R16 已验收路径的精确 blob 身份，**并把 R17 变更文件 `hermes_cli/kanban_db.py` 的修复前/修复后精确 blob 身份、精确 head 绑定链、以及合并后的安装 → 全新进程导入 → 有界角色分离 reload → 重载后健康读回 激活协议一并绑定**，声明（但不执行）后续角色分离的线上读回协议，包括 pids 资源增量、看板级零跨任务变更证明，以及三个指定残留行的确定性歧义所有权拒绝回执。本任务不执行该线上步骤。

---

## 1. Source binding — active editable source vs installed overlay

Two source locations exist and **must never be conflated**. They are bound separately
because the audit (t_85a264e0, finding F2) proved the previous activation packet treated
them as one.

| Role | Path | HEAD | Branch |
|------|------|------|--------|
| **Active editable import source** (the gateway venv's editable install maps `hermes_cli` here; a fresh process from the service working directory imports from this path) | `/root/hermes-agent-aion-lifecycle` | `2d63c805aa99d2c202fcb7efce4df34710b1e429` | `fix/aion-rl2-core-01-r16-archive-terminal-orphan-and-exact-residue-closure` |
| **Installed overlay** (declared install workspace; the primary checkout under `/usr/local`) | `/usr/local/lib/hermes-agent` | `b08767d444bf49ad70c11c8f44906c9ac4a7348e` | `main` |

The gateway venv's editable finder (`__editable___hermes_agent_*_finder.py`) maps every
`hermes_*` / `hermes_cli` package to `/root/hermes-agent-aion-lifecycle`. **The running
service imports the active source, not the installed overlay.**

---

## 2. Exact-head binding chain (pre-repair → corrected → merge)

This activation protocol must **never be followed against a stale head**. The
REQUEST_CHANGES finding F1 (audit `t_2ba77c00`, review 4936774783) was raised against the
pre-repair head and must be discharged on the corrected head, and every later activation
step is bound to the corrected head **plus its merge commit and both parents, read live
immediately before activation**.

| Binding | Value | How bound |
|---|---|---|
| **Pre-repair audited PR head** (REQUEST_CHANGES) | `c9ee8ad8ae0a609714c784c6b7205d5d67e43935` | immutable literal — audited by `t_2ba77c00`; verdict `REQUEST_CHANGES` on finding F1 only |
| Base | `c54bf57d56dc8e23635e1bf18b76e17c62467615` | immutable literal (`fork/main`) |
| **Corrected exact head** (this REVISION R1) | the single new commit on top of `c9ee8ad8…` that adds this manifest repair | bind-by-live-readback: recorded in this task's handoff, re-read from PR #14 immediately before activation; **must differ from `c9ee8ad8…`** |
| **Merge commit + both parents** | read live immediately before activation | mandatory readback — merge-commit SHA and its two parent SHAs (the `main` head and the corrected PR head) are recorded at activation time |

**Activation gate.** Immediately before any activation step, the activating role must read
from the live PR #14: (a) current PR head, (b) merge-commit SHA, (c) merge-commit parent
SHAs. Activation is **refused** unless the current PR head equals the corrected head, the
merge commit exists, and both parents are recorded. A mismatch (PR closed/merged against a
different head, branch drift, or a distinct live worker owning the same obligation) fails
closed — no stale-head reuse.

---

## 3. R17 `kanban_db.py` exact blob identity (pre/post, both locations)

The only runtime file changed by this PR is `hermes_cli/kanban_db.py`. Its identity is the
load-bearing proof that the later activation is executing the R17 repair and not the old
R16 bytes that still contain the broken `task_spawns` query.

| Location | Pre-repair blob (R16 — still contains `task_spawns` query) | Required post-install blob (R17) |
|---|---|---|
| Active editable source `/root/hermes-agent-aion-lifecycle/hermes_cli/kanban_db.py` | `af298790ba416292b898af397360cbeffa1d4f8f` | `717f81bf43ec7120ec22f9877ed5e355bdf7478d` |
| Installed overlay `/usr/local/lib/hermes-agent/hermes_cli/kanban_db.py` | `af298790ba416292b898af397360cbeffa1d4f8f` | `717f81bf43ec7120ec22f9877ed5e355bdf7478d` |

**Required code identity (constant):** `717f81bf43ec7120ec22f9877ed5e355bdf7478d`
(computed with `git hash-object` on `hermes_cli/kanban_db.py` at the exact head). This blob
is unchanged by REVISION R1 — only this manifest changed. Pre-repair, **both** locations
carry `af298790…`; after merge + install, **both** must carry `717f81bf…`. Any location
that does not match fails closed.

---

## 4. Four accepted R15/R16 path identities (exact blob binding)

These four paths are the accepted R15/R16 (terminal-run reconciliation + archive-orphan
closure) surface, verified by `git hash-object` on each worktree. The installed overlay
is **missing the fourth path (R15 test)** — this is stated explicitly and must NOT be
reported as installed.

| # | Path | Active source blob (`2d63c805…`) | Installed overlay (`b08767d4…`) |
|---|------|----------------------------------|--------------------------------|
| 1 | `hermes_cli/kanban.py` | `aeaa6562d5ab7ce7f4442bdb3818f9dadcd569f3` | `aeaa6562d5ab7ce7f4442bdb3818f9dadcd569f3` (present) |
| 2 | `hermes_cli/kanban_db.py` | `af298790ba416292b898af397360cbeffa1d4f8f` | `af298790ba416292b898af397360cbeffa1d4f8f` (present) |
| 3 | `tests/hermes_cli/test_kanban_terminal_run_reconciliation_r15.py` | `34db32a8c565674764ee2d6cc64663b9e4b7c52b` | **MISSING (not installed)** |
| 4 | `tests/hermes_cli/test_kanban_terminal_run_reconciliation_r16.py` | `3d4e243f6aa1d50deb3ccfc961840e0119c8ceee` | `3d4e243f6aa1d50deb3ccfc961840e0119c8ceee` (present) |

> Note: row 2 above is the **pre-R17** `kanban_db.py` identity (`af298790…`). The R17
> post-install identity is `717f81bf…` (Section 3). The four rows above are the accepted
> R15/R16 surface and must not be conflated with the R17 change.

---

## 5. R17 repair (this PR)

**Root cause.** PR #6 added `_cleanup_workspace_on_completion`, a second completion-time
process closure whose shared-dir `owned_pids` gate reads a `task_spawns` table that has
never existed in the canonical schema. Every dir-workspace completion raised
`sqlite3.OperationalError: no such table: task_spawns` inside the function's try/except,
logged `cleanup failed`, and silently skipped all process closure. Activation task
t_6e28b044 emitted exactly that line.

**Repair.** `_cleanup_workspace_on_completion` now derives task-owned PID/starttime
identity from the canonical `task_events` `spawned` event (the same source
`_cleanup_workspace` and `_set_worker_pid` already use) and never queries a nonexistent
table. The fail-closed fence is unchanged in shape:

- only the most recent `spawned` event with **both** a valid `pid` **and** a well-formed
  `/proc` `starttime` proves ownership;
- descendants are re-discovered via `_discover_descendant_pids(..., expected_starttime=…)`
  so a recycled PID is rejected;
- missing / legacy / malformed starttime and any bare-PID fallback never authorize a signal.

It returns a deterministic evidence dict (`outcome ∈ {success, safe_refusal,
identity_mismatch, no_task, no_workspace, internal_error}`) in addition to logging.

**Files changed (this PR):**
- `hermes_cli/kanban_db.py` — replace the `task_spawns` query with the canonical
  `task_events` `spawned` identity; deterministic evidence return.
- `tests/hermes_cli/test_kanban_dir_workspace_spawn_identity_r17.py` — RED→GREEN +
  adversarial coverage.
- `docs/kanban/aion-rl2-core-01-r17-dir-workspace-activation-manifest.md` — this contract
  (REVISION R1 adds the exact-head binding chain, R17 blob identity, and install / import /
  reload / health protocol).

---

## 6. RED / GREEN

Command (canonical):
```
scripts/run_tests.sh tests/hermes_cli/test_kanban_dir_workspace_spawn_identity_r17.py -q
```

- **RED (current `fork/main` c54bf57d56…): 1 passed, 9 failed** — every dir-workspace
  cleanup hit `complete_task workspace cleanup failed for task=…: no such table: task_spawns`.
- **GREEN (after repair): 10 passed, 0 failed.**

Adversarial coverage (acceptance matrix):

| Case | Outcome asserted |
|------|------------------|
| canonical schema has no `task_spawns` table | `task_spawns` absent; `task_events`/`task_runs` present |
| exact owned worker (valid spawn identity) | `success`, worker signalled |
| descendant (grandchild of owned worker) | `success`, worker + descendant signalled |
| unrelated same-dir worker (sibling) | `success` + `skipped_unowned ≥ 1`, unrelated preserved |
| no-spawn legacy task | `safe_refusal` (`no_provable_spawn_identity`), nothing signalled |
| legacy/missing starttime | `safe_refusal` (`no_provable_spawn_identity`), nothing signalled |
| recycled PID (starttime mismatch) | `identity_mismatch` (`recycled_pid`), nothing signalled |
| repeated / idempotent completion | second call is a safe no-op |
| scratch workspace (no ownership gate) | `success`, in-workspace process signalled |
| internal error does not block completion | `internal_error`; `complete_task` still returns True |

---

## 7. Direct resource deltas (pids.current / pids.events.max)

**Observed read-only during this task (informational, NOT a definitive delta):**
- `hermes-gateway-gm2.service` cgroup: `pids.current = 56`, `pids.max = 120`,
  `pids.events` = `max 0`.

This reading is taken while the R17 isolated test run is in-flight and is **not** the
required before/after pair. The definitive pre/post `pids.current` + `pids.events.max`
capture is part of the later role-separated live readback (Section 9) and is **not
executed here**.

---

## 8. Residue readback (read-only, zero mutation)

The three named residues are unchanged from the R16 audit — confirming this task
performed **zero committed mutation** on the live board:

| run | task | status | ended_at | claim_lock | worker_pid |
|-----|------|--------|----------|------------|------------|
| 664 | `t_8e8e8d62` | running | NULL | `VM-0-17-ubuntu:2889142` | 865894 |
| 2056 | `t_c0093dec` | running | NULL | `VM-0-17-ubuntu:961071` | NULL |
| 2061 | `t_bafab551` | running | NULL | `VM-0-17-ubuntu:961710` | NULL |

All three parent tasks are `done` with `current_run_id NULL`.

---

## 9. Later role-separated live readback + activation (DEFINED — NOT executed by this task)

The following protocol is specified here for the later role-separated (bafuxunan /
GemAION) post-merge activation and live readback. **This R17 task does not execute it.**
It binds the exact R17 bytes so a false activation PASS against pre-R17 code is impossible.

### 9.1 Install — bind the merged head into both locations (defined, not executed)

1. Confirm the activation gate from Section 2: current PR #14 head == corrected head, merge
   commit exists, both parents recorded. Refuse on any drift.
2. Install the merged head's `hermes_cli/kanban_db.py` into **both** locations so each
   carries the required R17 blob:
   - active editable source: `/root/hermes-agent-aion-lifecycle/hermes_cli/kanban_db.py`
   - installed overlay: `/usr/local/lib/hermes-agent/hermes_cli/kanban_db.py`
3. Verify with `git hash-object` on each file. Expected: **both** equal
   `717f81bf43ec7120ec22f9877ed5e355bdf7478d`. Any other value (e.g. the old
   `af298790…`) fails closed — do not proceed to import/reload.

### 9.2 Fresh service-context import — resolve intended active path + exact R17 bytes

In a **fresh** process using the gateway venv (NOT a long-lived interactive shell that may
have cached an old module), resolve the imported module to its on-disk path and hash the
resolved bytes:

```
HERMES_PYTHON=/root/hermes-agent-aion-lifecycle/.venv/bin/python
"$HERMES_PYTHON" - <<'PY'
import hashlib, pathlib
import hermes_cli.kanban_db as kdb
resolved = pathlib.Path(kdb.__file__).resolve()
data = resolved.read_bytes()
blob_sha1 = hashlib.sha1(b"blob %d%c" % (len(data), 0) + data).hexdigest()
print("resolved_path:", resolved)
print("blob_sha1:", blob_sha1)
PY
```

Expected: the printed `resolved_path` is `/root/hermes-agent-aion-lifecycle/hermes_cli/kanban_db.py`
(the intended active path) and the printed `blob_sha1` equals
`717f81bf43ec7120ec22f9877ed5e355bdf7478d` (git blob SHA-1 of those exact resolved bytes). The
hash is computed on the resolved `kdb.__file__` bytes in the same fresh process, so a wrong path
or wrong bytes cannot be silently accepted. **Mismatch (wrong path or wrong bytes) fails closed
and rolls back** — the R17 repair is not considered installed.

### 9.3 Bounded role-separated reload

One **scoped** reload of the affected service — never a full restart, never a stop, never a
process kill:

```
systemctl reload hermes-gateway-gm2.service
```

This step is **role-separated**: it is performed by the bafuxunan/GemAION audit lane at the
exact merged head, and is **bounded** to the single named service. 007 never performs it.

### 9.4 Immediate post-reload health + import readback

Immediately after the bounded reload, re-verify before any receipt is accepted:

1. Re-run the fresh-import proof (9.2): resolved path + `git hash-object` must still equal
   `717f81bf43ec7120ec22f9877ed5e355bdf7478d`.
2. Read service health: `systemctl is-active hermes-gateway-gm2.service` (expect `active`),
   and read the cgroup `pids.current` / `pids.events` counters (Section 7) to confirm the
   reload did not leak processes.
3. **Mismatch fails closed and rolls back** — revert the installed bytes to the prior blob
   and re-run the fresh-import proof until the intended state is restored. No refusal /
   readback receipt may be accepted against bytes that are not `717f81bf…`.

### 9.5 Refusal / residue readback (unchanged from R16, only after 9.1–9.4 pass)

1. **Exact per-target refusal receipt.** For each of `t_8e8e8d62` / `t_c0093dec` /
   `t_bafab551`, invoke the installed `repair_terminal_orphan_runs` / CLI path against the
   named task and capture the deterministic refusal payload. Expected: every row has a
   non-null `claim_lock` (run 664 also a non-null `worker_pid`), so execution must fail
   closed with `ambiguous_live_ownership` and **zero mutation**. One receipt per target,
   bound to the exact run id + task id + claim_lock + worker_pid, re-read after posting.
2. **Direct pre/post `pids.current` + `pids.events.max`.** Read both counters immediately
   before and after the (refused) invocation from
   `/sys/fs/cgroup/system.slice/hermes-gateway-gm2.service/`; assert no growth
   attributable to the readback.
3. **Board-wide zero-cross-task-mutation guard.** Snapshot the full `task_runs`
   (`status`, `ended_at`, `worker_pid`, `claim_lock`) and `tasks` (`status`,
   `current_run_id`, `worker_pid`, `claim_lock`) before and after; assert byte-identical
   equality for every row, not just the three named residues.
4. **Zero committed mutation.** The entire live readback runs in a read-only / rollback
   transaction (`mode=ro` or `BEGIN` … `ROLLBACK`); no `UPDATE`/`DELETE`/`INSERT` is ever
   committed. No raw claim clearing.
5. **Role separation.** 007 never self-audits; the refusal receipts and readback are
   produced and read back by the bafuxunan/GemAION audit lane at the exact merged head.

---

## 10. Limits / rollback / stop conditions

- One PR maximum; smallest existing-path change (`kanban_db.py` + focused tests + this
  manifest). No new table, migration, service, queue, scheduler, daemon, or control plane.
- Rollback: revert this PR; the previous behavior (silent `task_spawns` failure) is the
  only thing removed, and the fail-closed fence is the same one already live in
  `_cleanup_workspace`.
- **Install/import/reload rollback:** any mismatch in Sections 9.1–9.4 (wrong head, wrong
  blob, wrong resolved path, post-reload health failure) fails closed — revert the
  installed bytes to the prior blob (`af298790…`) and re-verify before retrying.
- Stop conditions (this task): origin/main or the existing-path identity source could not
  be bound safely; repair required a new schema/table/control plane; any live/runtime/DB/
  external mutation became necessary; or the safe PID/starttime fence could not be proven.
  None of these were triggered.
- Forbidden claims: no Resource Lifecycle PASS, no Reliability Burn-down DONE, no
  residue-closed, no unattended-ready.
