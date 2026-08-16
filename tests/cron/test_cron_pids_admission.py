"""R31 — builtin cron admission containment (shared-cgroup pids.max).

Deterministic RED/GREEN for the fail-closed/defer containment added to the
built-in ``tick`` submit boundary: when the gateway service cgroup has a finite
pids ceiling and the free headroom cannot safely cover a due execution's
worker/provider/subprocess fan-out, the job is deferred — not submitted, not
advanced, no execution row manufactured — and retried on the next cadence once
headroom recovers. On hosts with no finite pids ceiling (unlimited / cgroup v1
/ unavailable / malformed metrics) ordinary behaviour is preserved.
"""

import pathlib

import cron.scheduler as sched
from cron import scheduler


# ---------------------------------------------------------------------------
# cgroup v2 pids headroom parsing
# ---------------------------------------------------------------------------

class TestPidsHeadroom:
    def _write_cgroup_v2(self, tmp_path, pids_max, pids_current):
        proc = tmp_path / "self_cgroup"
        proc.write_text("0::/test.slice/hermes-gateway.service\n")
        base = tmp_path / "cgroup-root" / "test.slice" / "hermes-gateway.service"
        base.mkdir(parents=True, exist_ok=True)
        (base / "pids.max").write_text(pids_max)
        (base / "pids.current").write_text(pids_current)
        return proc, base

    @staticmethod
    def _patch_path(monkeypatch, tmp_path):
        def _path(p):
            if p == "/proc/self/cgroup":
                return tmp_path / "self_cgroup"
            if p.startswith("/sys/fs/cgroup"):
                rel = p[len("/sys/fs/cgroup"):].lstrip("/")
                return tmp_path / "cgroup-root" / rel
            return pathlib.Path(p)

        monkeypatch.setattr(scheduler, "Path", _path)

    def test_finite_headroom(self, tmp_path, monkeypatch):
        self._write_cgroup_v2(tmp_path, "120\n", "44\n")
        self._patch_path(monkeypatch, tmp_path)
        assert scheduler._cron_pids_headroom() == 76

    def test_unlimited_max_returns_none(self, tmp_path, monkeypatch):
        self._write_cgroup_v2(tmp_path, "max\n", "44\n")
        self._patch_path(monkeypatch, tmp_path)
        assert scheduler._cron_pids_headroom() is None

    def test_clamped_at_zero_when_current_exceeds_max(self, tmp_path, monkeypatch):
        self._write_cgroup_v2(tmp_path, "120\n", "130\n")
        self._patch_path(monkeypatch, tmp_path)
        assert scheduler._cron_pids_headroom() == 0

    def test_malformed_max_returns_none(self, tmp_path, monkeypatch):
        self._write_cgroup_v2(tmp_path, "garbage\n", "44\n")
        self._patch_path(monkeypatch, tmp_path)
        assert scheduler._cron_pids_headroom() is None

    def test_malformed_current_returns_none(self, tmp_path, monkeypatch):
        self._write_cgroup_v2(tmp_path, "120\n", "not-a-number\n")
        self._patch_path(monkeypatch, tmp_path)
        assert scheduler._cron_pids_headroom() is None

    def test_missing_proc_self_cgroup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            scheduler, "Path",
            lambda p: tmp_path / "does-not-exist" if p == "/proc/self/cgroup" else pathlib.Path(p),
        )
        assert scheduler._cron_pids_headroom() is None

    def test_not_cgroup_v2_returns_none(self, tmp_path, monkeypatch):
        proc = tmp_path / "self_cgroup"
        proc.write_text("3:cpu:/user.slice\n")  # cgroup v1 — no "0::" unified row
        monkeypatch.setattr(
            scheduler, "Path",
            lambda p: proc if p == "/proc/self/cgroup" else pathlib.Path(p),
        )
        assert scheduler._cron_pids_headroom() is None

    def test_no_pids_controller_returns_none(self, tmp_path, monkeypatch):
        proc = tmp_path / "self_cgroup"
        proc.write_text("0::/test.slice/hermes-gateway.service\n")

        def _path(p):
            if p == "/proc/self/cgroup":
                return proc
            if p.startswith("/sys/fs/cgroup"):
                return tmp_path / "missing-root" / p[len("/sys/fs/cgroup"):].lstrip("/")
            return pathlib.Path(p)

        monkeypatch.setattr(scheduler, "Path", _path)
        assert scheduler._cron_pids_headroom() is None


# ---------------------------------------------------------------------------
# admission reserve / budget resolution
# ---------------------------------------------------------------------------

class TestPidsAdmissionBudget:
    def test_unbounded_when_headroom_none(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_cron_pids_admission_reserve", lambda: 16)
        monkeypatch.setattr(scheduler, "_cron_pids_headroom", lambda: None)
        assert scheduler._cron_pids_admission_budget() is None

    def test_zero_when_below_reserve(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_cron_pids_admission_reserve", lambda: 16)
        monkeypatch.setattr(scheduler, "_cron_pids_headroom", lambda: 15)
        assert scheduler._cron_pids_admission_budget() == 0

    def test_floor_division_budget(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_cron_pids_admission_reserve", lambda: 16)
        monkeypatch.setattr(scheduler, "_cron_pids_headroom", lambda: 40)
        assert scheduler._cron_pids_admission_budget() == 2

    def test_disabled_when_reserve_nonpositive(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_cron_pids_admission_reserve", lambda: 0)
        assert scheduler._cron_pids_admission_budget() is None

    def test_reserve_reads_config(self, monkeypatch):
        monkeypatch.setattr(
            scheduler, "load_config",
            lambda: {"cron": {"pids_admission_reserve": 32}},
        )
        assert scheduler._cron_pids_admission_reserve() == 32

    def test_reserve_default_when_missing(self, monkeypatch):
        monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {}})
        assert (
            scheduler._cron_pids_admission_reserve()
            == scheduler._CRON_PIDS_ADMISSION_RESERVE_DEFAULT
        )

    def test_reserve_default_on_error(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(scheduler, "load_config", _boom)
        assert (
            scheduler._cron_pids_admission_reserve()
            == scheduler._CRON_PIDS_ADMISSION_RESERVE_DEFAULT
        )


# ---------------------------------------------------------------------------
# tick(): builtin submit-boundary RED/GREEN behaviour
# ---------------------------------------------------------------------------

class TestTickPidsAdmission:
    @staticmethod
    def _job(jid, **extra):
        job = {"id": jid, "name": jid, "workdir": None}
        job.update(extra)
        return job

    @staticmethod
    def _install_tick_stubs(monkeypatch, jobs, budget):
        submitted = []
        advanced = []
        executions = []

        monkeypatch.setattr(sched, "get_due_jobs", lambda: list(jobs))
        monkeypatch.setattr(sched, "advance_next_run", lambda jid: advanced.append(jid) or None)
        monkeypatch.setattr(
            sched, "create_execution",
            lambda jid, **kw: executions.append((jid, kw.get("source"))) or {"id": f"exec-{jid}"},
        )
        monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: submitted.append(job["id"]) or True)
        # raising=False so the RED case also runs (and fails for the right
        # reason) against the pre-fix code, which has no admission hook yet.
        monkeypatch.setattr(sched, "_cron_pids_admission_budget", lambda: budget, raising=False)
        # Avoid the real post-tick MCP orphan sweep touching live processes.
        monkeypatch.setattr("tools.mcp_tool._kill_orphaned_mcp_children", lambda: None)
        return {"submitted": submitted, "advanced": advanced, "executions": executions}

    def test_sufficient_headroom_submits_exactly_once(self, monkeypatch):
        jobs = [self._job("a")]
        st = self._install_tick_stubs(monkeypatch, jobs, budget=None)
        assert sched.tick(verbose=False) == 1
        assert st["submitted"] == ["a"]
        assert st["advanced"] == ["a"]
        assert st["executions"] == [("a", "builtin")]

    def test_insufficient_headroom_defers_no_submit(self, monkeypatch):
        # RED: a due job must NOT be submitted, advanced, or given an execution
        # row when the admission budget is exhausted — it stays due and retries
        # on the next cadence.
        jobs = [self._job("a")]
        st = self._install_tick_stubs(monkeypatch, jobs, budget=0)
        assert sched.tick(verbose=False) == 0
        assert st["submitted"] == []
        assert st["advanced"] == []
        assert st["executions"] == []

    def test_recovery_submits_deferred_job_exactly_once(self, monkeypatch):
        # Phase 1: insufficient headroom -> defer (nothing submitted).
        jobs = [self._job("a")]
        st = self._install_tick_stubs(monkeypatch, jobs, budget=0)
        assert sched.tick(verbose=False) == 0
        assert st["submitted"] == []
        assert st["executions"] == []

        # Phase 2: headroom recovers -> the SAME due obligation submits exactly
        # once, with no replacement task/execution duplication.
        st2 = self._install_tick_stubs(monkeypatch, jobs, budget=None)
        assert sched.tick(verbose=False) == 1
        assert st2["submitted"] == ["a"]
        assert st2["executions"] == [("a", "builtin")]

    def test_multiple_due_jobs_bounded_by_budget(self, monkeypatch):
        jobs = [self._job(f"j{i}") for i in range(4)]
        st = self._install_tick_stubs(monkeypatch, jobs, budget=2)
        assert sched.tick(verbose=False) == 2
        assert len(st["submitted"]) == 2
        assert len(st["advanced"]) == 2
        assert len(st["executions"]) == 2
        assert len(set(st["submitted"])) == 2  # no duplicate submission

    def test_deferred_workdir_job_not_submitted(self, monkeypatch):
        # Containment applies to the sequential (workdir) partition too.
        jobs = [self._job("w", workdir="/tmp/somewhere")]
        st = self._install_tick_stubs(monkeypatch, jobs, budget=0)
        assert sched.tick(verbose=False) == 0
        assert st["submitted"] == []
        assert st["advanced"] == []

    def test_unlimited_budget_preserves_parallel_behaviour(self, monkeypatch):
        jobs = [self._job("a"), self._job("b"), self._job("c")]
        st = self._install_tick_stubs(monkeypatch, jobs, budget=None)
        assert sched.tick(verbose=False) == 3
        assert sorted(st["submitted"]) == ["a", "b", "c"]
        assert sorted(st["advanced"]) == ["a", "b", "c"]
