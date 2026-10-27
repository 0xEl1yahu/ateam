"""ateam run: the scheduler, end to end on a scratch target with fake BUILD/TEST sessions and the real verify,
refresh, merge and integrate. Contracts: C-0001 (a), C-0002 (b, depends on a), C-0003 (c)."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from ateam.cli import Context
from ateam.config import NamedCheck
from ateam.contracts.store import load_contracts
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir
from ateam.plans.store import load_plans
from ateam.state.db import StateDB
from test_integrate import _commit_on_main, _fake_planner, _set_config, run

NAMES = {"C-0001": "a", "C-0002": "b", "C-0003": "c"}


def _report3():
    def contract(name, depends_on):
        return {"title": f"Add {name}", "paths": [f"src/{name}/**", f"tests/test_{name}.py"], "incidental": [], "depends_on": depends_on,
                "acceptance": [{"id": "A1", "statement": f"{name}() works", "check": {"type": "test", "ref": f"tests/test_{name}.py::test_{name}"}}],
                "checks": ["unit"], "allocation": {"complexity": "low", "risk": "low", "uncertainty": "low", "reasoning": "small",
                                                   "tier": {"build": "light", "test": "light", "investigate": "none"}}}
    return {"title": "Three parts", "contracts": [contract("a", []), contract("b", [0]), contract("c", [])],
            "proposed_records": [], "unknowns": [], "notes": "n"}


@pytest.fixture
def plan3(target_repo, monkeypatch):
    paths = TargetPaths.from_root(target_repo)
    _set_config(paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=2)
    _fake_planner(monkeypatch, _report3())
    assert run("plan", "three parts", "--no-review", "--target", str(target_repo)).exit_code == 0
    assert run("approve", "P-0001", "--skip-records", "--target", str(target_repo)).exit_code == 0
    return SimpleNamespace(root=target_repo, paths=paths, main=GitRepo(target_repo).head())


class FakeWorkers:
    """Stands in for `ateam worker run`: records an attempt, commits the contract's files on BUILD, writes the result
    file. `script[(cid, role, n)]` or `script[(cid, role)]` is a dict merged into the result, or a callable."""

    def __init__(self, script=None, delay=0.0):
        self.script, self.delay = script or {}, delay
        self.calls: list[tuple] = []
        self.intervals: list[tuple] = []
        self.lock = threading.Lock()

    def __call__(self, target, cid, role, worktree, base, tier, prompt, result_file):
        with self.lock:
            n = sum(1 for c in self.calls if c[0] == cid and c[1] == role)
            self.calls.append((cid, role, tier, prompt))
        paths = TargetPaths.from_root(target)
        with StateDB(home_dir(target) / "state.db") as db:   # in the scheduler's thread, so closed here
            db.init_schema()
            intent = GitRepo(paths.intent)
            before, t0 = intent.head(), time.monotonic()
            attempt = db.start_attempt(cid, role, tier or "light", "fake", {})
            behaviour = self.script.get((cid, role, n), self.script.get((cid, role), {}))
            override = behaviour(self, cid, role, worktree, n) if callable(behaviour) else dict(behaviour)
            result = {"code": 0, "outcome": "completed", "subtype": "success", "error": None, "cost_usd": 0.1,
                      "session_id": "s", "test_outcome": None, "test_class": None, "report": {}}
            if role == "build" and override.pop("commit", True):
                name = NAMES[cid]
                (worktree / "src" / name).mkdir(parents=True, exist_ok=True)
                (worktree / "src" / name / f"{name}.py").write_text(f"def {name}():\n    return '{name}'  # {n}\n")
                (worktree / "tests" / f"test_{name}.py").write_text(f"def test_{name}():\n    assert True\n")
                g = GitRepo(worktree); g.run("add", "-A"); g.run("commit", "-q", "-m", f"{name} attempt {n}")
            result.update(override)
            if role == "test" and result["code"] == 0:
                result["test_outcome"] = "completed"
            time.sleep(self.delay)
            if result["outcome"] != "interrupted":
                db.finish_attempt(attempt, result["outcome"], None, 1, 1, result["cost_usd"], 1, 0.1, "s")
            self.intervals.append((cid, role, t0, time.monotonic(), before, intent.head()))
            result_file.write_text(json.dumps(result))
            return result

    def tiers(self, cid, role="build"):
        return [c[2] for c in self.calls if c[0] == cid and c[1] == role]


class FakeInvestigator:
    """Stands in for `ateam worker pause-investigate`: writes findings (or a note) on the open pause record."""

    def __init__(self, report=None, outcome="completed", write=True):
        self.calls, self.outcome, self.write = [], outcome, write
        self.report = report or {"summary": "s", "evidence": [], "failure_class": "implementation",
                                 "needs_owner_decision": None, "suggested_instruction": "use x"}

    def __call__(self, target, plan_id, cid, worktree, result_file):
        with StateDB(home_dir(target) / "state.db") as db:
            db.init_schema()
            self.calls.append((cid, db.feature_status(plan_id), worktree))
            if self.write:
                pause = db.open_pause(plan_id)
                findings = self.report if self.outcome == "completed" else None
                db.set_pause_findings(pause["pause_id"], cid, None, findings, None if findings else f"{self.outcome}: x")
        r = {"code": 0 if self.outcome == "completed" else 1, "outcome": self.outcome, "attempt_id": None,
             "cost_usd": 0.2, "error": None}
        result_file.write_text(json.dumps(r))
        return r


def _run(p, launch, stop=None, **kw):
    from ateam.scheduler import run_plan
    kw.setdefault("investigate", FakeInvestigator())
    out: list[str] = []
    summary = run_plan(lambda: Context(p.root), "P-0001", out.append, stop or threading.Event(), launch=launch, **kw)
    return summary, out


def _db(p):
    db = StateDB(home_dir(p.root) / "state.db"); db.init_schema()
    return db


def _last_reason(p):
    return _db(p).transitions("feature", "P-0001")[-1]["reason"]


def test_happy_path_merges_at_verify_and_stops_at_merge_proposed(plan3):
    s, out = _run(plan3, FakeWorkers())
    assert s.outcome == "merge_proposed", out
    cs = load_contracts(plan3.paths.contracts)
    assert all(cs[c].status == "integrated" for c in NAMES) and all(_db(plan3).contract_status(c) == "integrated" for c in NAMES)
    assert load_plans(plan3.paths.plans)["P-0001"].status == "merge_proposed" and s.proposal and s.proposal.is_file()
    assert (home_dir(plan3.root) / "worktrees" / "C-0002" / "src" / "a" / "a.py").is_file()   # b cut after a merged
    assert GitRepo(plan3.root).rev_parse("main") == plan3.main and _db(plan3).run_lock_info() is None
    assert any("C-0001 build completed" in line for line in out)


def _lock_held(p):
    with _db(p).conn as c:
        c.execute("INSERT INTO run_lock (singleton, plan_id, acquired_at, pid) VALUES (1, 'P-0001', 'then', 1)")
    return "held by P-0001 (pid 1"


def _rejected(p):
    assert run("reject", "P-0001", "no", "--target", str(p.root)).exit_code == 0
    return "plan_rejected"


def _dirty_intent(p):
    (p.paths.intent / "stray.txt").write_text("x\n")
    return "uncommitted"


def _hand_edit(p):
    from ateam.contracts.store import save_contract
    from ateam.intent import commit_intent
    c = load_contracts(p.paths.contracts)["C-0003"]
    save_contract(p.paths.contracts, c.model_copy(update={"title": "edited"}))
    commit_intent(p.paths, "hand edit", ["contracts"])
    return "C-0003 differs from its frozen hash"


@pytest.mark.parametrize("setup", [_lock_held, _rejected, _dirty_intent, _hand_edit])
def test_preflight_refusals_and_lock(setup, plan3):
    from ateam.scheduler import RunRefused
    expected = setup(plan3)
    fake = FakeWorkers()
    import re
    with pytest.raises(RunRefused, match=re.escape(expected)):
        _run(plan3, fake)
    assert fake.calls == []
    if setup is _lock_held:
        assert _db(plan3).run_lock_info()["pid"] == 1        # never released or stolen by the refused run


def test_dry_run_writes_nothing_and_shows_worst_case(plan3):
    fake = FakeWorkers()
    intent_head = GitRepo(plan3.paths.intent).head()
    s, out = _run(plan3, fake, dry_run=True)
    text = "\n".join(out)
    assert s.outcome == "dry_run" and fake.calls == []
    assert "wave 1: C-0001, C-0003" in text and "wave 2: C-0002" in text
    attempts = load_contracts(plan3.paths.contracts)["C-0001"].budget.attempts   # the planner's light-tier budget
    per_contract = (1.0 + 1.0) + (attempts - 1) * (2.0 + 2.0)   # first attempt light, the rest escalated to standard
    assert f"worst case ${3 * per_contract:.2f}" in text and "run budget $10.00" in text
    assert GitRepo(plan3.paths.intent).head() == intent_head and not GitRepo(plan3.root).branch_exists("ateam/P-0001")
    assert all(_db(plan3).attempts_for(c) == [] for c in NAMES) and _db(plan3).feature_status("P-0001") == "plan_approved"


def test_max_workers_overlap_and_no_session_during_exclusive(plan3):
    fake = FakeWorkers(delay=0.4)
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    builds = {cid: (t0, t1) for cid, role, t0, t1, *_ in fake.intervals if role == "build"}
    a, c = builds["C-0001"], builds["C-0003"]
    assert a[0] < c[1] and c[0] < a[1]                                   # the two independent BUILDs overlapped
    assert all(before == after for *_, before, after in fake.intervals)  # no intent commit during any session


def test_scope_breach_retries_then_pauses_scope_exceeded(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers({("C-0001", "build"): {"code": 3, "outcome": "scope_breach"}})
    s, out = _run(plan3, fake)
    assert s.outcome == "paused" and len(fake.tiers("C-0001")) == load_contracts(plan3.paths.contracts)["C-0001"].budget.attempts
    assert _last_reason(plan3).startswith("scope_exceeded: C-0001")
    assert load_plans(plan3.paths.plans)["P-0001"].status == "paused" == _db(plan3).feature_status("P-0001")


def test_error_max_turns_escalates_once(plan3):
    fake = FakeWorkers({("C-0001", "build", 0): {"code": 1, "outcome": "failed", "subtype": "error_max_turns", "commit": False}})
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    assert fake.tiers("C-0001") == [None, "standard"], fake.calls
    assert fake.tiers("C-0001", role="test") == ["standard"]          # TEST never below BUILD
    prompts = [c[3] for c in fake.calls if c[:2] == ("C-0001", "build")]
    assert prompts[0] is None and prompts[1], fake.calls   # the retry carries a prompt


def test_requirements_pauses_decision_change_needed(plan3):
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}})
    s, out = _run(plan3, fake)
    assert s.outcome == "paused" and _last_reason(plan3).startswith("decision_change_needed: C-0001")


def test_failed_test_verdict_retries_build_with_the_evidence(plan3):
    report = {"verdicts": [{"id": "A1", "verdict": "not_met", "evidence": "a() returns None"}], "requirements_unclear": False}
    fake = FakeWorkers({("C-0001", "test", 0): {"code": 5, "test_outcome": "failed", "test_class": "implementation", "report": report}})
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    retry_prompt = [c[3] for c in fake.calls if c[:2] == ("C-0001", "build")][1]
    assert "A1" in retry_prompt and "a() returns None" in retry_prompt


def test_budget_stop_leaves_the_plan_executing(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=2, run_budget_usd=1.2)
    fake = FakeWorkers(delay=0.2)
    s, out = _run(plan3, fake)
    assert s.outcome == "budget" and _db(plan3).feature_status("P-0001") == "executing"   # owner, 2026-10-26
    assert len(fake.calls) == 3        # $1 caps, $0.10 spent each: a session fits while spend + $1 <= $1.20 (0, 0.1, 0.2)
    assert s.spent_usd <= 1.2 and any("run budget" in m for m in s.messages)


def test_an_in_flight_reservation_does_not_stop_the_run(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=2, run_budget_usd=2.0)
    s, out = _run(plan3, FakeWorkers(delay=0.2))
    assert s.outcome == "merge_proposed", out      # two $1 caps in flight wait for each other instead of stopping


def test_merge_conflict_pauses_environment_failure_contract_verified(plan3):
    from ateam.integrate import integration_worktree

    def conflicting_merge_lands_first(fake, cid, role, worktree, n):
        wt = GitRepo(integration_worktree(plan3.paths, "P-0001"))
        (wt.root / "src" / "a").mkdir(parents=True, exist_ok=True)
        (wt.root / "src" / "a" / "a.py").write_text("A = 'someone else'\n")
        wt.run("add", "-A"); wt.run("commit", "-q", "-m", "a conflicting merge")
        return {}

    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    s, out = _run(plan3, FakeWorkers({("C-0001", "build"): conflicting_merge_lands_first}))
    assert s.outcome == "paused" and _last_reason(plan3) == "environment_failure: C-0001 conflicts in src/a/a.py"
    assert _db(plan3).contract_status("C-0001") == "verified"


def test_stop_interrupts_closes_attempts_and_rerun_continues(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    stop = threading.Event()

    def ctrl_c(fake, cid, role, worktree, n):
        stop.set()
        return {"code": 130, "outcome": "interrupted", "commit": False}

    s, out = _run(plan3, FakeWorkers({("C-0001", "build", 0): ctrl_c}), stop=stop)
    db = _db(plan3)
    assert s.outcome == "interrupted" and db.open_attempts(list(NAMES)) == []
    assert db.attempts_for("C-0001")[0]["outcome"] == "interrupted"
    assert db.feature_status("P-0001") == "executing" and db.run_lock_info() is None
    base = db.get_meta("base:C-0001")
    s2, out2 = _run(plan3, FakeWorkers())
    assert s2.outcome == "merge_proposed", out2
    assert _db(plan3).get_meta("base:C-0001") == base


def test_rerun_by_state(plan3):
    from ateam.scheduler import RunRefused
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    _run(plan3, FakeWorkers({("C-0001", "build"): {"code": 3, "outcome": "scope_breach"}}))
    with pytest.raises(RunRefused, match=r"P-0001 is paused \(scope_exceeded"):
        _run(plan3, FakeWorkers())


def test_merge_proposed_plan_is_refused(plan3):
    from ateam.scheduler import RunRefused
    assert _run(plan3, FakeWorkers())[0].outcome == "merge_proposed"
    with pytest.raises(RunRefused, match="merge_proposed"):
        _run(plan3, FakeWorkers())


def test_crash_between_contract_commit_and_store_is_reconciled(plan3, monkeypatch):
    from ateam.state.db import StateDB as DB
    real = DB.transition
    crashed = {"done": False}

    def crash_once(self, kind, id, to, by, reason=None):
        if kind == "contract" and id == "C-0003" and to == "integrated" and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("power cut")
        return real(self, kind, id, to, by, reason)

    with monkeypatch.context() as m:
        m.setattr(DB, "transition", crash_once)
        with pytest.raises(RuntimeError, match="power cut"):
            _run(plan3, FakeWorkers())
    assert load_contracts(plan3.paths.contracts)["C-0003"].status == "integrated" and _db(plan3).contract_status("C-0003") == "verified"
    assert _db(plan3).run_lock_info() is None
    s, out = _run(plan3, FakeWorkers())
    assert s.outcome == "merge_proposed", out
    assert any("reconciled: C-0003" in line for line in out)


def test_stall_names_blockers(plan3):
    with _db(plan3).conn as c:
        c.execute("UPDATE contracts SET status='paused' WHERE contract_id='C-0002'")
    from ateam.contracts.store import save_contract
    from ateam.contracts.model import ContractStatus
    from ateam.intent import commit_intent
    b = load_contracts(plan3.paths.contracts)["C-0002"]
    save_contract(plan3.paths.contracts, b.model_copy(update={"status": ContractStatus.paused}))
    commit_intent(plan3.paths, "b paused", ["contracts"])
    s, out = _run(plan3, FakeWorkers())
    assert s.outcome == "stalled" and "C-0002 (paused)" in " ".join(s.messages)


# ---- final review fixes

def test_interrupted_attempts_do_not_count(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    for _ in range(2):                           # light contracts allow 2 BUILD attempts
        stop = threading.Event()

        def ctrl_c(fake, cid, role, worktree, n, stop=stop):
            stop.set()
            return {"code": 130, "outcome": "interrupted", "commit": False}

        assert _run(plan3, FakeWorkers({("C-0001", "build", 0): ctrl_c}), stop=stop)[0].outcome == "interrupted"
    s, out = _run(plan3, FakeWorkers())
    assert s.outcome == "merge_proposed", out


def test_no_session_starts_after_stop_while_waiting_at_the_gate(plan3):
    from ateam.scheduler import run_child
    stop = threading.Event()
    stamp = {}

    def child(argv, result_file):
        if "verify" in argv and "C-0001" in argv and not stop.is_set():
            stop.set(); stamp["at"] = time.monotonic()
        return run_child(argv, result_file)

    fake = FakeWorkers(delay=0.3)
    s, out = _run(plan3, fake, stop=stop, child=child)
    assert s.outcome == "interrupted"
    assert all(t0 < stamp["at"] for _cid, _role, t0, *_ in fake.intervals)   # nothing launched after Ctrl-C


def test_dependent_waits_for_its_dependency_to_be_integrated(plan3, monkeypatch):
    import ateam.scheduler as sch
    from ateam.integrate import merge_contract as real_merge

    def crash_before_merge(ctx, wt, plan_id, cid, echo):
        if cid == "C-0001":
            raise RuntimeError("killed between verify and merge")
        return real_merge(ctx, wt, plan_id, cid, echo)

    with monkeypatch.context() as m:
        m.setattr(sch, "merge_contract", crash_before_merge)
        with pytest.raises(RuntimeError):
            _run(plan3, FakeWorkers())
    assert _db(plan3).contract_status("C-0001") == "verified"

    def slow_merge(ctx, wt, plan_id, cid, echo):
        time.sleep(0.5 if cid == "C-0001" else 0)
        return real_merge(ctx, wt, plan_id, cid, echo)

    with monkeypatch.context() as m:
        m.setattr(sch, "merge_contract", slow_merge)
        s, out = _run(plan3, FakeWorkers())
    assert s.outcome == "merge_proposed", out
    assert (home_dir(plan3.root) / "worktrees" / "C-0002" / "src" / "a" / "a.py").is_file()


def test_a_verified_result_without_the_store_agreeing_is_not_merged(plan3):
    from ateam.scheduler import run_child

    def lying_verify(argv, result_file):
        if "verify" in argv:
            return {"code": 0, "status": "verified", "failure_class": None, "reasons": []}
        return run_child(argv, result_file)

    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    s, out = _run(plan3, FakeWorkers(), child=lying_verify)
    assert s.outcome == "paused" and _last_reason(plan3).startswith("environment_failure: C-0001 verify reported verified")
    assert _db(plan3).contract_status("C-0001") == "ready"


def test_each_run_writes_its_own_result_directory(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    stop = threading.Event()

    def ctrl_c(fake, cid, role, worktree, n):
        stop.set(); return {"code": 130, "outcome": "interrupted", "commit": False}

    _run(plan3, FakeWorkers({("C-0001", "build", 0): ctrl_c}), stop=stop)
    _run(plan3, FakeWorkers())
    runs = [d for d in (home_dir(plan3.root) / "runs" / "P-0001").iterdir() if d.is_dir()]
    assert len(runs) == 2


def test_final_integrate_runs_as_a_child(plan3):
    from ateam.scheduler import run_child
    seen = []

    def spy(argv, result_file):
        seen.append(argv)
        return run_child(argv, result_file)

    s, out = _run(plan3, FakeWorkers(), child=spy)
    assert s.outcome == "merge_proposed", out
    assert any("integrate" in a for a in seen)
    assert s.proposal is not None and s.proposal.is_file()


def test_tampered_session_pauses_verification_unresolvable(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    s, out = _run(plan3, FakeWorkers({("C-0001", "build"): {"outcome": "tamper"}}))
    assert s.outcome == "paused" and _last_reason(plan3).startswith("verification_unresolvable: C-0001")


def test_environment_failure_at_test_retries_test_not_build(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers({("C-0001", "test", 0): {"code": 1, "outcome": "failed", "subtype": None, "error": "transport"}})
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    assert len(fake.tiers("C-0001")) == 1 and len(fake.tiers("C-0001", role="test")) == 2


def test_run_refuses_while_a_plan_is_being_planned(plan3):
    import os
    from ateam.scheduler import RunRefused
    db = _db(plan3)
    db.transition("feature", "P-0009", "requested", by="owner", reason="r")
    db.transition("feature", "P-0009", "investigating", by="ateam")
    db.set_meta("planning:P-0009", str(os.getpid()))
    fake = FakeWorkers()
    with pytest.raises(RunRefused, match=r"P-0009 is being planned \(pid \d+\); wait for it to finish"):
        _run(plan3, fake)
    assert fake.calls == [] and db.run_lock_info() is None      # the lock is released afterwards


# ---- pause records, attempt windows, owner stop (pause and resume, task 4)

def test_a_pause_is_recorded_with_its_contract_and_evidence(plan3):
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}})
    s, out = _run(plan3, fake)
    db = _db(plan3); p = db.open_pause("P-0001")
    assert s.outcome == "paused" and p["reason"] == "decision_change_needed" and not p["conflict"]
    [c] = p["contracts"]
    assert c["contract_id"] == "C-0001" and c["evidence"]["test_class"] == "requirements"
    assert c["contract_hash"] == db.contract_hash("C-0001") and db.contract_status("C-0001") == "ready"


def test_merge_conflict_pause_is_marked_conflict(plan3):
    from ateam.integrate import integration_worktree

    def conflicting_merge_lands_first(fake, cid, role, worktree, n):
        wt = GitRepo(integration_worktree(plan3.paths, "P-0001"))
        (wt.root / "src" / "a").mkdir(parents=True, exist_ok=True)
        (wt.root / "src" / "a" / "a.py").write_text("A = 'someone else'\n")
        wt.run("add", "-A"); wt.run("commit", "-q", "-m", "a conflicting merge")
        return {}

    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    s, out = _run(plan3, FakeWorkers({("C-0001", "build"): conflicting_merge_lands_first}))
    p = _db(plan3).open_pause("P-0001")
    assert s.outcome == "paused" and p["conflict"] is True and p["contracts"][0]["contract_id"] == "C-0001"


def test_a_window_refreshes_attempts_and_escalation(plan3):
    from ateam.plans.ops import set_plan_status
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)   # C-0003 never starts
    turns = {"code": 1, "outcome": "failed", "subtype": "error_max_turns", "commit": False}
    s, out = _run(plan3, FakeWorkers({("C-0001", "build"): turns}))     # escalates once, then budget_exhausted
    assert s.outcome == "paused" and _last_reason(plan3).startswith("budget_exhausted: C-0001")
    db = _db(plan3); pause = db.open_pause("P-0001")
    used = len(db.attempts_for("C-0001", role="build"))
    db.resume_pause(pause["pause_id"], ["C-0001"])
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["executing"], by="t")
    fake2 = FakeWorkers()
    s2, out2 = _run(plan3, fake2)
    assert s2.outcome == "merge_proposed", out2
    assert fake2.tiers("C-0001")[0] is None                               # the frozen tier again
    assert len(_db(plan3).attempts_for("C-0001", role="build")) == used + 1


def test_owner_stop_pauses_owner_stopped(plan3):
    def owner_stop(fake, cid, role, worktree, n):
        with _db(plan3) as db:   # in a scheduler thread, so closed here
            db.request_stop("P-0001")
        time.sleep(0.8)                                                    # the loop polls within 0.5 s
        return {"code": 130, "outcome": "interrupted", "commit": False}

    s, out = _run(plan3, FakeWorkers({("C-0001", "build", 0): owner_stop}))
    db = _db(plan3)
    assert s.outcome == "paused" and _last_reason(plan3) == "owner_stopped: stopped by the owner"
    assert db.open_pause("P-0001")["contracts"] == [] and not db.stop_requested("P-0001")
    assert db.open_attempts(list(NAMES)) == [] and db.run_lock_info() is None


def test_owner_stop_during_an_exclusive_step_finishes_it_without_tamper(plan3):
    from ateam.integrate import integration_worktree
    from ateam.scheduler import run_child
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)

    def child(argv, result_file):
        if "verify" in argv:
            with _db(plan3) as db:   # in a scheduler thread, so closed here
                db.request_stop("P-0001")
            time.sleep(0.8)                                                # the loop sees the request while verify runs
        return run_child(argv, result_file)

    s, out = _run(plan3, FakeWorkers(), child=child)
    db = _db(plan3)
    assert s.outcome == "paused" and _last_reason(plan3) == "owner_stopped: stopped by the owner", out
    assert db.contract_status("C-0001") in ("verified", "integrated")      # the verify finished and was recorded
    assert all(a["outcome"] != "tamper" for a in db.telemetry_rows())
    git_dir = GitRepo(integration_worktree(plan3.paths, "P-0001")).run("rev-parse", "--git-dir").strip()
    from pathlib import Path
    assert not (Path(git_dir) / "index.lock").exists()


def test_a_stop_request_left_from_before_the_run_is_cleared(plan3):
    _db(plan3).request_stop("P-0001")
    s, out = _run(plan3, FakeWorkers())
    assert s.outcome == "merge_proposed" and not _db(plan3).stop_requested("P-0001")


def test_default_launch_registers_and_interrupt_all_signals_only_children(plan3, monkeypatch, tmp_path):
    import signal
    from ateam import scheduler
    monkeypatch.setattr(scheduler, "ateam_argv", lambda: ["/bin/sh", "-c", "sleep 30", "child"])
    box = {}
    t = threading.Thread(target=lambda: box.update(r=scheduler.default_launch(plan3.root, "C-0001", "build", plan3.root, "x",
                                                                              None, None, tmp_path / "r.json")))
    t.start()
    deadline = time.monotonic() + 5
    while not scheduler._Children.interrupt_all() and time.monotonic() < deadline:
        time.sleep(0.05)
    t.join(5)
    assert not t.is_alive() and box["r"]["outcome"] == "failed" and str(-signal.SIGINT) in box["r"]["error"]
    assert scheduler._Children.interrupt_all() == 0


def test_paused_plan_refusal_names_resume(plan3):
    from ateam.scheduler import RunRefused
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}})
    _run(plan3, fake)
    with pytest.raises(RunRefused, match=r"resume it with: ateam resume P-0001"):
        _run(plan3, FakeWorkers())


def test_a_runs_directory_is_never_reused_within_one_second(tmp_path):
    from ateam.scheduler import fresh_dir
    a, b = fresh_dir(tmp_path, "20261027T100000Z-1"), fresh_dir(tmp_path, "20261027T100000Z-1")
    assert a != b and a.is_dir() and b.is_dir() and a.name == "20261027T100000Z-1"


# ---- automatic pause investigations (pause and resume, task 6)

_REQUIREMENTS = {("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}}


def test_an_ateam_pause_investigates_each_paused_contract_once(plan3):
    inv = FakeInvestigator()
    s, out = _run(plan3, FakeWorkers(_REQUIREMENTS), investigate=inv)
    assert s.outcome == "paused" and [c[0] for c in inv.calls] == ["C-0001"] and inv.calls[0][1] == "paused"
    assert inv.calls[0][2] == home_dir(plan3.root) / "worktrees" / "C-0001"
    assert _db(plan3).open_pause("P-0001")["contracts"][0]["findings"]["suggested_instruction"] == "use x"
    assert any("C-0001 investigate completed" in line for line in out)


def test_owner_stop_is_not_investigated(plan3):
    inv = FakeInvestigator()

    def owner_stop(fake, cid, role, worktree, n):
        with _db(plan3) as db:
            db.request_stop("P-0001")
        time.sleep(0.8)
        return {"code": 130, "outcome": "interrupted", "commit": False}

    s, out = _run(plan3, FakeWorkers({("C-0001", "build", 0): owner_stop}), investigate=inv)
    assert s.outcome == "paused" and _last_reason(plan3).startswith("owner_stopped") and inv.calls == []


def test_a_tampered_investigation_leaves_the_pause_standing(plan3):
    s, out = _run(plan3, FakeWorkers(_REQUIREMENTS), investigate=FakeInvestigator(outcome="tamper"))
    c = _db(plan3).open_pause("P-0001")["contracts"][0]
    assert s.outcome == "paused" and c["findings"] is None and c["note"].startswith("tamper")
    assert _db(plan3).feature_status("P-0001") == "paused"


def test_a_crashed_investigation_gets_a_note(plan3):
    s, out = _run(plan3, FakeWorkers(_REQUIREMENTS), investigate=FakeInvestigator(outcome="failed", write=False))
    c = _db(plan3).open_pause("P-0001")["contracts"][0]
    assert c["findings"] is None and c["note"] == "investigation failed: no findings written"


def test_investigation_that_does_not_fit_the_budget_leaves_a_note(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1, run_budget_usd=2.0)
    inv = FakeInvestigator()
    s, out = _run(plan3, FakeWorkers(_REQUIREMENTS), investigate=inv)
    c = _db(plan3).open_pause("P-0001")["contracts"][0]
    assert s.outcome == "paused" and inv.calls == [] and c["note"].startswith("run budget $2.00 would be exceeded")


def test_no_worktree_means_no_investigation(plan3, monkeypatch):
    from ateam import worktrees as wt_mod
    from ateam.worktrees import WorktreeError

    def no_worktree(*a, **kw):
        raise WorktreeError("cannot make it")

    monkeypatch.setattr("ateam.scheduler.contract_worktree", no_worktree)
    inv = FakeInvestigator()
    s, out = _run(plan3, FakeWorkers(), investigate=inv)
    c = next(c for c in _db(plan3).open_pause("P-0001")["contracts"] if c["contract_id"] == "C-0001")
    assert inv.calls == [] and c["note"] == "no worktree to investigate"


def test_unit_tests_cannot_start_a_real_investigation(tmp_path):
    from ateam import scheduler
    with pytest.raises(AssertionError, match="real pause investigation"):
        scheduler.default_investigate(tmp_path, "P-0001", "C-0001", tmp_path, tmp_path / "r.json")


def test_no_session_starts_after_an_owner_stop_is_seen(plan3):
    """The poll halts the run as it sets stop: a pipeline moving from BUILD to TEST right after the stop is seen never
    launches TEST (final review, 2026-10-27)."""
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    stop = threading.Event()

    def stop_then_finish(fake, cid, role, worktree, n):
        with _db(plan3) as db:   # in a scheduler thread, so closed here
            db.request_stop("P-0001")
        deadline = time.monotonic() + 5
        while not stop.is_set() and time.monotonic() < deadline:   # the poll has seen the request
            time.sleep(0.01)
        return {}                                                   # BUILD completed: the pipeline moves on at once

    fake = FakeWorkers({("C-0001", "build", 0): stop_then_finish})
    s, out = _run(plan3, fake, stop=stop)
    assert s.outcome == "paused" and _last_reason(plan3).startswith("owner_stopped")
    assert [c[1] for c in fake.calls] == ["build"], fake.calls


# ---- a BUILD that records no attempt can never loop (live test, 2026-10-27)

class _NoAttemptLaunch:
    """A session that fails before `ateam worker run` records an attempt; raises after 10 calls so a loop shows up."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, target, cid, role, worktree, base, tier, prompt, result_file):
        self.calls.append((cid, role))
        if len(self.calls) > 10:
            raise RuntimeError(f"looping: {len(self.calls)} launches")
        r = {"code": 1, "subtype": None, "error": None, "cost_usd": 0.0, "session_id": None, "test_outcome": None,
             "test_class": None, "report": None, **self.result}
        result_file.write_text(json.dumps(r))
        return r


def test_a_failing_setup_pauses_after_one_launch(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    launch = _NoAttemptLaunch({"outcome": "setup_failed", "error": "setup failed: false (exit 1)\n"})
    s, out = _run(plan3, launch)
    assert s.outcome == "paused" and _last_reason(plan3).startswith("environment_failure: C-0001 build setup_failed")
    assert launch.calls == [("C-0001", "build")]


def test_builds_that_record_no_attempt_still_count_toward_the_limit(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    launch = _NoAttemptLaunch({"outcome": "failed", "error": "crashed before the attempt was recorded"})
    s, out = _run(plan3, launch)
    allowed = load_contracts(plan3.paths.contracts)["C-0001"].budget.attempts
    assert s.outcome == "paused" and len(launch.calls) == allowed, launch.calls


def test_run_closes_the_contexts_it_opens(plan3):
    from ateam.scheduler import run_plan
    from dbleaks import is_open

    made: list[tuple[Context, int]] = []

    def factory() -> Context:
        c = Context(plan3.root)
        made.append((c, threading.get_ident()))
        return c

    out: list[str] = []
    s = run_plan(factory, "P-0001", out.append, threading.Event(), launch=FakeWorkers(), investigate=FakeInvestigator())
    assert s.outcome == "merge_proposed"
    assert len({t for _, t in made}) > 1   # run_plan's own context and the pipeline threads' contexts
    me = threading.get_ident()
    assert [c for c, t in made if is_open(c.db, t == me)] == []
    assert _db(plan3).run_lock_info() is None
