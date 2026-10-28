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
LETTERS = {f"C-{i:04d}": name for i, name in enumerate("abcdefgh", 1)}   # the files FakeWorkers commits for each contract


def _contract(name, depends_on, shared=False):
    return {"title": f"Add {name}", "paths": [*(["src/shared/**"] if shared else []), f"src/{name}/**", f"tests/test_{name}.py"],
            "incidental": [], "depends_on": depends_on,
            "acceptance": [{"id": "A1", "statement": f"{name}() works", "check": {"type": "test", "ref": f"tests/test_{name}.py::test_{name}"}}],
            "checks": ["unit"], "allocation": {"complexity": "low", "risk": "low", "uncertainty": "low", "reasoning": "small",
                                               "tier": {"build": "light", "test": "light", "investigate": "none"}}}


def _report3():
    return {"title": "Three parts", "contracts": [_contract("a", []), _contract("b", [0]), _contract("c", [])],
            "proposed_records": [], "unknowns": [], "notes": "n"}


def _report_of(*specs):
    """One contract per (shared, depends_on) spec, named a, b, c...: src/<name>/** and its test, plus src/shared/**
    when shared (so the shared ones form one lane); depends_on holds indices, as the planner reports them."""
    return {**_report3(), "contracts": [_contract(name, deps, shared) for (shared, deps), name in zip(specs, "abcdefgh")]}


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
            self.calls.append((cid, role, tier, prompt, worktree, base))
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
            commit = override.pop("commit", True)
            if role == "build" and commit and prompt and "merge conflict" in prompt:
                g = GitRepo(worktree)   # the rebase BUILD: resolve every conflicted file, then conclude the merge
                for rel in [p for p in g.run("diff", "--name-only", "--diff-filter=U").splitlines() if p]:
                    (worktree / rel).write_text(f"# {rel} resolved by {cid}\n")
                g.run("add", "-A"); g.run("commit", "-q", "--no-edit")
            elif role == "build" and commit:
                name = LETTERS[cid]
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
    # b depends on a, but their files do not overlap: two lanes, two worktrees (owner decision 4, 2026-10-28 design)
    worktrees = home_dir(plan3.root) / "worktrees"
    assert (worktrees / "C-0001" / "src" / "a" / "a.py").is_file() and (worktrees / "C-0002" / "src" / "b" / "b.py").is_file()
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
    assert "3 lane(s), up to 2 at once" in text and out[2:5] == ["lane 1: C-0001", "lane 2: C-0002", "lane 3: C-0003"]
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
    assert (home_dir(plan3.root) / "worktrees" / "C-0002" / "src" / "b" / "b.py").is_file()   # b is a lane of its own


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


# ---- lanes (2026-10-27-lanes-design.md, decision 1)

def _c(paths, incidental=(), depends_on=()):
    return SimpleNamespace(paths=list(paths), incidental=list(incidental), depends_on=list(depends_on))


@pytest.mark.parametrize("contracts, files, expected", [
    pytest.param({"A": _c(["src/m/*.py"]), "B": _c(["src/m/x.py"])}, ["src/m/x.py"], [["A", "B"]], id="overlapping globs"),
    pytest.param({"A": _c(["src/x/**"]), "B": _c(["src/x/new.py"])}, [], [["A", "B"]], id="glob vs literal new file"),
    pytest.param({"A": _c(["src/y/**"]), "B": _c(["src/y/**"])}, [], [["A", "B"]], id="identical globs, no files"),
    # Owner decision 4 of the 2026-10-28 design: only overlapping files join lanes, so dependents of one contract fan out
    pytest.param({"A": _c(["a/**"]), "B": _c(["b/**"], depends_on=["A"])}, ["a/1", "b/1"], [["A"], ["B"]], id="dependency only"),
    pytest.param({"A": _c(["a/**"], incidental=["shared.cfg"]), "B": _c(["shared.cfg"])}, ["shared.cfg"], [["A", "B"]],
                 id="incidental counts"),
    pytest.param({"A": _c(["a/**"]), "B": _c(["b/**"])}, ["a/1", "b/1"], [["A"], ["B"]], id="disjoint"),
    # Files join A-B-C through B; D depends on A but shares no file (owner decision 4, 2026-10-28 design)
    pytest.param({"C": _c(["c/**"], incidental=["b/1"]), "B": _c(["b/**"], incidental=["a/1"], depends_on=["C"]), "A": _c(["a/**"]),
                  "D": _c(["d/**"], depends_on=["A"])},
                 ["a/1", "b/1", "c/1", "d/1"], [["A", "C", "B"], ["D"]], id="transitive, ordered by deps then id"),
    pytest.param({"A": _c(["a/**"], depends_on=["Z"])}, ["a/1"], [["A"]], id="dependency outside the plan ignored"),
])
def test_lanes(contracts, files, expected):
    from ateam.scheduler import lanes
    assert lanes(contracts, sorted(contracts), files) == expected


def _report_lanes():
    r = _report3()
    for c, paths in zip(r["contracts"], (["src/shared/**", "src/a/**", "tests/test_a.py"], ["src/shared/**", "src/b/**", "tests/test_b.py"],
                                         ["src/c/**", "tests/test_c.py"])):
        c["paths"], c["depends_on"] = paths, []
    return r


def _approve_lanes(target_repo, monkeypatch, check="true", max_workers=2, report=None):
    """C-0001 and C-0002 share src/shared/** (one lane); C-0003 is a lane of one. No dependencies. The check's command
    is frozen into the contracts at approval."""
    paths = TargetPaths.from_root(target_repo)
    _set_config(paths, checks=[NamedCheck(name="unit", cmd=check)], max_workers=max_workers)
    _fake_planner(monkeypatch, report or _report_lanes())
    assert run("plan", "lanes", "--no-review", "--target", str(target_repo)).exit_code == 0
    r = run("approve", "P-0001", "--skip-records", "--target", str(target_repo))
    assert r.exit_code == 0
    return SimpleNamespace(root=target_repo, paths=paths, main=GitRepo(target_repo).head(), approve_output=r.output)


@pytest.fixture
def plan_lanes(target_repo, monkeypatch):
    return _approve_lanes(target_repo, monkeypatch)


def test_approve_prints_the_lanes(plan_lanes):
    assert "2 lane(s), up to 2 at once" in plan_lanes.approve_output
    assert "lane 1: C-0001 -> C-0002" in plan_lanes.approve_output and "lane 2: C-0003" in plan_lanes.approve_output


# Owner decision 7 of the 2026-10-28 design: a follower's base is the integration tip, not its predecessor's head.
def test_a_lane_runs_in_one_worktree_and_the_follower_is_based_on_the_integration_tip(plan_lanes):
    seen = {}

    def note_predecessor(fake, cid, role, worktree, n):
        with StateDB(home_dir(plan_lanes.root) / "state.db") as db:   # the scheduler's thread: closed here
            seen["C-0001"] = db.contract_status("C-0001")
        return {}

    fake = FakeWorkers({("C-0002", "build", 0): note_predecessor})
    s, out = _run(plan_lanes, fake)
    assert s.outcome == "merge_proposed", out
    lane = home_dir(plan_lanes.root) / "worktrees" / "C-0001"
    b_build = next(c for c in fake.calls if c[0] == "C-0002" and c[1] == "build")
    assert b_build[4] == lane and b_build[5] == _db(plan_lanes).get_meta("base:C-0002")
    assert GitRepo(lane).is_ancestor(_db(plan_lanes).latest_checkpoint("C-0001")["head"], b_build[5])
    assert _db(plan_lanes).get_meta("lane:C-0002") == "C-0001" and not (home_dir(plan_lanes.root) / "worktrees" / "C-0002").exists()
    assert seen == {"C-0001": "integrated"}


def test_disjoint_lanes_overlap_in_time(plan_lanes):
    fake = FakeWorkers(delay=0.3)
    s, out = _run(plan_lanes, fake)
    assert s.outcome == "merge_proposed", out
    span = {cid: (t0, t1) for cid, role, t0, t1, *_ in fake.intervals if role == "build"}
    (a0, a1), (c0, c1) = span["C-0001"], span["C-0003"]
    assert a0 < c1 and c0 < a1


def test_a_failure_mid_lane_blocks_the_rest_of_that_lane(plan_lanes):
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "outcome": "failed", "test_class": "requirements"}})
    s, out = _run(plan_lanes, fake)
    assert s.outcome == "paused", out
    assert not any(c[0] == "C-0002" for c in fake.calls)
    pause = _db(plan_lanes).open_pause("P-0001")
    assert pause["reason"] == "decision_change_needed" and pause["detail"].endswith("; blocks C-0002 (same lane)")


def test_a_follower_with_untracked_leftovers_pauses(target_repo, monkeypatch):
    plan_lanes = _approve_lanes(target_repo, monkeypatch, check="touch leftover.txt", max_workers=1)
    fake = FakeWorkers()
    s, out = _run(plan_lanes, fake)
    assert s.outcome == "paused", out
    assert not any(c[0] == "C-0002" for c in fake.calls)
    pause = _db(plan_lanes).open_pause("P-0001")
    assert pause["reason"] == "environment_failure"
    assert pause["detail"] == "C-0002: the lane worktree has files left by an earlier contract (leftover.txt)"


def test_setup_reruns_on_each_new_base(plan_lanes, tmp_path):
    from ateam.config import load_config
    from ateam.runner.run import ensure_setup

    counter = tmp_path / "setup-count"
    _set_config(plan_lanes.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=2, setup=[f"echo x >> {counter}"])

    def with_setup(extra):
        def build(fake, cid, role, worktree, n):
            with StateDB(home_dir(plan_lanes.root) / "state.db") as db:   # the scheduler's thread: closed here
                ensure_setup(worktree, load_config(plan_lanes.paths), db, db.get_meta(f"base:{cid}"))
            return dict(extra)
        return build

    scope = {"code": 3, "outcome": "scope_breach", "commit": False}
    fake = FakeWorkers({("C-0001", "build"): with_setup({}), ("C-0002", "build"): with_setup({}),
                        ("C-0003", "build", 0): with_setup(scope), ("C-0003", "build", 1): with_setup({})})
    s, out = _run(plan_lanes, fake)
    assert s.outcome == "merge_proposed", out
    # C-0001, C-0002 on its new base, C-0003 once for two attempts, and the integration worktree at the end
    assert len(fake.tiers("C-0003")) == 2 and counter.read_text().count("x") == 4


def test_investigation_runs_in_the_lane_worktree_and_status_names_it(plan_lanes):
    inv = FakeInvestigator()
    fake = FakeWorkers({("C-0002", "test"): {"code": 5, "outcome": "failed", "test_class": "requirements"}})
    _set_config(plan_lanes.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    s, out = _run(plan_lanes, fake, investigate=inv)
    assert s.outcome == "paused", out
    lane = home_dir(plan_lanes.root) / "worktrees" / "C-0001"
    assert [(c[0], c[2]) for c in inv.calls] == [("C-0002", lane)]
    r = run("status", "P-0001", "--target", str(plan_lanes.root))
    assert f"worktree: {lane}" in r.output
    assert "lane 1: C-0001 -> C-0002" in r.output and "lane 2: C-0003" in r.output


# ---- the rebase BUILD (2026-10-27-lanes-design.md, rulings 6-9 and 12)

def _land_on_integration(p, rel, text):
    """Another merge lands on ateam/P-0001 that touches `rel`, as a lane the overlap test missed would."""
    from ateam.integrate import integration_worktree
    wt = GitRepo(integration_worktree(p.paths, "P-0001"))
    (wt.root / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt.root / rel).write_text(text)
    wt.run("add", "-A"); wt.run("commit", "-q", "-m", f"another merge touches {rel}")
    return wt.head()


def _conflict_during(p, rel="src/a/a.py", text="A = 'someone else'\n", extra=None):
    def lands(fake, cid, role, worktree, n):
        fake.tips = getattr(fake, "tips", []) + [_land_on_integration(p, rel, text)]
        return dict(extra or {})
    return lands


def _builds(fake, cid):
    return [c for c in fake.calls if c[0] == cid and c[1] == "build"]


def _rebase_builds(fake, cid):
    return [c for c in _builds(fake, cid) if c[3] and "merge conflict" in c[3]]


def _interrupting(stop):
    def interrupted(fake, cid, role, worktree, n):
        stop.set()
        return {"code": 130, "outcome": "interrupted", "commit": False}
    return interrupted


def test_a_merge_conflict_gets_one_rebase_build_then_test_verify_and_merge(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3)})
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    db = _db(plan3)
    rebases = _rebase_builds(fake, "C-0001")
    assert len(rebases) == 1 and "src/a/a.py" in rebases[0][3] and len(_builds(fake, "C-0001")) == 2
    assert [c[1] for c in fake.calls if c[0] == "C-0001"] == ["build", "test", "build", "test"]
    reopened = [t for t in db.transitions("contract", "C-0001") if t["from_status"] == "verified" and t["to_status"] == "building"]
    assert len(reopened) == 1 and reopened[0]["reason"].startswith("rebase onto")
    assert db.get_meta("base:C-0001") == fake.tips[0] and rebases[0][5] == fake.tips[0]
    assert load_contracts(plan3.paths.contracts)["C-0001"].status == "integrated" and db.open_pause("P-0001") is None


def test_a_second_conflict_pauses_as_a_conflict(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3),
                        ("C-0001", "build", 1): _conflict_during(plan3, text="A = 'someone else again'\n")})
    s, out = _run(plan3, fake)
    pause = _db(plan3).open_pause("P-0001")
    assert s.outcome == "paused" and pause["conflict"] is True, out
    assert pause["contracts"][0]["detail"].startswith("C-0001 conflicts in src/a/a.py; again after its rebase BUILD")
    assert _db(plan3).contract_status("C-0001") == "verified" and len(_rebase_builds(fake, "C-0001")) == 1


def test_a_conflict_with_no_attempt_left_pauses(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    allowed = load_contracts(plan3.paths.contracts)["C-0001"].budget.attempts
    script = {("C-0001", "build", n): {"code": 3, "outcome": "scope_breach", "commit": False} for n in range(allowed - 1)}
    script[("C-0001", "build", allowed - 1)] = _conflict_during(plan3)
    fake = FakeWorkers(script)
    s, out = _run(plan3, fake)
    pause = _db(plan3).open_pause("P-0001")
    assert s.outcome == "paused" and pause["conflict"] is True, out
    assert pause["contracts"][0]["detail"].startswith("C-0001 conflicts in src/a/a.py; no BUILD attempt left")
    assert _rebase_builds(fake, "C-0001") == []


def test_a_conflict_in_an_incidental_path_pauses_without_a_rebase(target_repo, monkeypatch):
    report = _report_lanes()
    report["contracts"][0]["incidental"] = ["shared.lock"]
    p = _approve_lanes(target_repo, monkeypatch, max_workers=1, report=report)

    def lock_and_conflict(fake, cid, role, worktree, n):
        (worktree / "shared.lock").write_text("mine\n")
        return _conflict_during(p, rel="shared.lock", text="theirs\n")(fake, cid, role, worktree, n)

    fake = FakeWorkers({("C-0001", "build", 0): lock_and_conflict})
    s, out = _run(p, fake)
    pause = _db(p).open_pause("P-0001")
    assert s.outcome == "paused" and pause["conflict"] is True, out
    assert "shared.lock are outside the contract's paths" in pause["contracts"][0]["detail"]
    assert _rebase_builds(fake, "C-0001") == []


def test_a_rebase_build_that_leaves_the_merge_open_pauses(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3), ("C-0001", "build", 1): {"commit": False}})
    s, out = _run(plan3, fake)
    pause = _db(plan3).open_pause("P-0001")
    assert s.outcome == "paused" and pause["conflict"] is True, out
    assert pause["contracts"][0]["detail"].startswith("C-0001 rebase BUILD did not conclude the merge")
    assert [c[1] for c in fake.calls if c[0] == "C-0001"] == ["build", "test", "build"]


def _failed_test_after_rebase(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3),
                        ("C-0001", "test", 1): {"code": 5, "outcome": "failed", "test_class": "implementation"}})
    return fake, _run(plan3, fake)


def test_a_failed_test_after_the_rebase_pauses_without_retry(plan3):
    fake, (s, out) = _failed_test_after_rebase(plan3)
    pause = _db(plan3).open_pause("P-0001")
    assert s.outcome == "paused" and pause["reason"] == "environment_failure", out
    assert len(_rebase_builds(fake, "C-0001")) == 1 and len(_builds(fake, "C-0001")) == 2
    assert _db(plan3).contract_status("C-0001") == "building"


def test_a_failed_test_after_the_rebase_resumes_with_an_ordinary_build(plan3):
    _failed_test_after_rebase(plan3)
    r = run("resume", "P-0001", "--no-run", "--target", str(plan3.root))
    assert r.exit_code == 0, r.output
    fake = FakeWorkers()
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    prompt = _builds(fake, "C-0001")[0][3]
    assert "merge conflict" not in prompt and "did not pass" in prompt


def test_a_rebase_interrupted_by_stop_continues_on_rerun(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    stop = threading.Event()
    s, out = _run(plan3, FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3), ("C-0001", "build", 1): _interrupting(stop)}),
                  stop=stop)
    assert s.outcome == "interrupted" and _db(plan3).contract_status("C-0001") == "building", out
    fake = FakeWorkers()
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    assert "merge conflict" in _builds(fake, "C-0001")[0][3]


def test_a_crash_after_preparing_the_rebase_still_runs_it(plan3, monkeypatch):
    import ateam.scheduler as sch
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)

    def killed(*a, **k):
        raise SystemExit("killed between preparing the rebase and reopening the contract")

    with monkeypatch.context() as m:
        m.setattr(sch, "reopen_for_rebase", killed)
        with pytest.raises(SystemExit):
            _run(plan3, FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3)}))
    assert _db(plan3).contract_status("C-0001") == "verified"
    assert GitRepo(home_dir(plan3.root) / "worktrees" / "C-0001").merge_in_progress()
    fake = FakeWorkers()
    s, out = _run(plan3, fake)
    assert s.outcome == "merge_proposed", out
    assert len(_rebase_builds(fake, "C-0001")) == 1


def test_a_rebase_that_cannot_start_pauses(plan3, monkeypatch):
    import ateam.scheduler as sch
    from ateam.gitops import GitError
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)

    def refuses(*a, **k):
        raise GitError("untracked working tree files would be overwritten by merge")

    with monkeypatch.context() as m:
        m.setattr(sch, "prepare_rebase", refuses)
        fake = FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3)})
        s, out = _run(plan3, fake)
    pause = _db(plan3).open_pause("P-0001")
    assert s.outcome == "paused" and pause["conflict"] is True, out
    assert pause["contracts"][0]["detail"].startswith("C-0001 rebase could not start: untracked working tree files")
    assert _db(plan3).contract_status("C-0001") == "verified" and _rebase_builds(fake, "C-0001") == []


# Owner decision 7 of the 2026-10-28 design: a follower's base is the integration tip, which holds the other merge.
def test_a_follower_after_a_rebased_lane_head_is_based_on_the_integration_tip(plan_lanes):
    fake = FakeWorkers({("C-0001", "build", 0): _conflict_during(plan_lanes)})
    s, out = _run(plan_lanes, fake)
    assert s.outcome == "merge_proposed", out
    db = _db(plan_lanes)
    lane = home_dir(plan_lanes.root) / "worktrees" / "C-0001"
    b_build = _builds(fake, "C-0002")[0]
    assert b_build[4] == lane and b_build[5] == db.get_meta("base:C-0002")
    assert GitRepo(lane).is_ancestor(fake.tips[0], b_build[5])   # the integration tip holds the other lane's merge


def test_an_integrating_rerun_continues_a_rebase(plan3):
    from ateam.plans.ops import set_plan_status
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    stop = threading.Event()
    s, out = _run(plan3, FakeWorkers({("C-0002", "build", 0): _conflict_during(plan3, rel="src/b/b.py"),
                                      ("C-0002", "build", 1): _interrupting(stop)}), stop=stop)
    db = _db(plan3)   # C-0002 merges last: the others are integrated, as a hand-run integrate would need
    assert s.outcome == "interrupted" and db.contract_status("C-0002") == "building", out
    assert db.contract_status("C-0001") == db.contract_status("C-0003") == "integrated"
    ctx = Context(plan3.root)
    set_plan_status(ctx.paths, ctx.db, load_plans(ctx.paths.plans)["P-0001"], ["integrating"], by="integrate")
    s, out = _run(plan3, FakeWorkers())
    assert s.outcome == "merge_proposed", out


def test_the_p0018_shape_prints_two_lanes(target_repo, monkeypatch):
    """The live P-0018 shape on a scratch target: two contracts in src/ateam/cli.py, one in src/ateam/runner/policy.py."""
    _commit_on_main(target_repo, "src/ateam/cli.py", "def main():\n    pass\n")
    _commit_on_main(target_repo, "src/ateam/runner/policy.py", "POLICY = {}\n")
    report = _report3()
    for c, paths in zip(report["contracts"], (["src/ateam/cli.py", "tests/test_a.py"], ["src/ateam/cli.py", "tests/test_b.py"],
                                              ["src/ateam/runner/policy.py", "tests/test_c.py"])):
        c["paths"], c["depends_on"] = paths, []
    p = _approve_lanes(target_repo, monkeypatch, report=report)
    s, out = _run(p, FakeWorkers(), dry_run=True)
    text = "\n".join(out)
    assert s.outcome == "dry_run"
    assert "2 lane(s), up to 2 at once" in text and "lane 1: C-0001 -> C-0002" in text and "lane 2: C-0003" in text


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



# ---- final review fixes (owner rulings 14 and 15, 2026-10-27)

@pytest.mark.parametrize("retest", [{}, {"code": 5, "outcome": "failed", "test_class": "implementation"}], ids=["passes", "fails"])
def test_a_rerun_after_a_concluded_rebase_tests_and_verifies_without_a_build(plan3, retest):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    stop = threading.Event()
    s, out = _run(plan3, FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3), ("C-0001", "test", 1): _interrupting(stop)}),
                  stop=stop)
    assert s.outcome == "interrupted" and _db(plan3).contract_status("C-0001") == "building", out
    fake = FakeWorkers({("C-0001", "test"): retest})
    s, out = _run(plan3, fake)
    assert _builds(fake, "C-0001") == [] and [c[1] for c in fake.calls if c[0] == "C-0001"] == ["test"]
    if retest:
        pause = _db(plan3).open_pause("P-0001")
        assert s.outcome == "paused" and pause["reason"] == "environment_failure", out
        assert "after its rebase BUILD" in pause["contracts"][0]["detail"]
    else:
        assert s.outcome == "merge_proposed", out


def test_a_rebase_build_that_fails_names_its_outcome(plan3):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers({("C-0001", "build", 0): _conflict_during(plan3),
                        ("C-0001", "build", 1): {"commit": False, "code": 1, "outcome": "setup_failed", "error": "setup failed: make deps"}})
    s, out = _run(plan3, fake)
    detail = _db(plan3).open_pause("P-0001")["contracts"][0]["detail"]
    assert detail.startswith("C-0001 rebase BUILD did not conclude the merge (build setup_failed: setup failed: make deps)"), detail


def test_an_uneditable_conflict_is_marked_in_the_pause(target_repo, monkeypatch):
    report = _report_lanes()
    report["contracts"][0]["incidental"] = ["shared.lock"]
    p = _approve_lanes(target_repo, monkeypatch, max_workers=1, report=report)

    def lock_and_conflict(fake, cid, role, worktree, n):
        (worktree / "shared.lock").write_text("mine\n")
        return _conflict_during(p, rel="shared.lock", text="theirs\n")(fake, cid, role, worktree, n)

    _run(p, FakeWorkers({("C-0001", "build", 0): lock_and_conflict}))
    assert _db(p).open_pause("P-0001")["contracts"][0]["evidence"]["uneditable"] == ["shared.lock"]
    r = run("resume", "P-0001", "--no-run", "--target", str(p.root))
    assert r.exit_code == 4 and "shared.lock" in r.output and "C-0001 may not edit" in r.output, r.output
    out = run("status", "P-0001", "--target", str(p.root)).output
    assert "shared.lock, which C-0001 may not edit" in out and "reject the plan" in out and "ateam resume P-0001" not in out, out


# ---- fan-out and the lane hold rule (2026-10-28 design, owner decision 4)

def _spans(fake, cid):
    times = [(t0, t1) for c, _role, t0, t1, *_ in fake.intervals if c == cid]
    return min(t for t, _ in times), max(t for _, t in times)


def _crashing(fake, cid, role, worktree, n):
    raise RuntimeError("power cut")


def test_dependents_of_one_contract_fan_out_after_it_is_integrated(target_repo, monkeypatch):
    p = _approve_lanes(target_repo, monkeypatch, max_workers=3, report=_report_of((False, []), (False, [0]), (False, [0]), (False, [0])))
    assert "4 lane(s), up to 3 at once" in p.approve_output
    seen = {}

    def note(fake, cid, role, worktree, n):
        with StateDB(home_dir(p.root) / "state.db") as db:   # the scheduler's thread: closed here
            seen[cid] = db.contract_status("C-0001")
        return {}

    dependents = ("C-0002", "C-0003", "C-0004")
    fake = FakeWorkers({(cid, "build", 0): note for cid in dependents}, delay=0.3)
    s, out = _run(p, fake)
    assert s.outcome == "merge_proposed", out
    assert seen == {cid: "integrated" for cid in dependents}
    span = {cid: (t0, t1) for cid, role, t0, t1, *_ in fake.intervals if role == "build"}
    assert all(span[x][0] < span[y][1] and span[y][0] < span[x][1] for x, y in [dependents[:2], dependents[1:], dependents[::2]])
    assert all(_builds(fake, cid)[0][4] == home_dir(p.root) / "worktrees" / cid for cid in dependents)


@pytest.mark.parametrize("stopper", ["interrupted", "crash"])
def test_a_started_lane_member_holds_the_lane_across_a_rerun(target_repo, monkeypatch, stopper):
    """C-0001 is a lane of its own; C-0002 (depending on C-0001) and C-0003 share a lane. C-0003 starts first and is
    stopped. On the re-run C-0001 is integrated first, so C-0002 could start, but C-0003 has started and holds the lane."""
    p = _approve_lanes(target_repo, monkeypatch, report=_report_of((False, []), (True, [0]), (True, [])))
    stop, idle = threading.Event(), {("C-0001", "build", 0): {"code": 130, "outcome": "interrupted", "commit": False}}
    if stopper == "interrupted":
        assert _run(p, FakeWorkers({**idle, ("C-0003", "build", 0): _interrupting(stop)}), stop=stop)[0].outcome == "interrupted"
    else:
        with pytest.raises(RuntimeError, match="power cut"):
            _run(p, FakeWorkers({**idle, ("C-0003", "build", 0): _crashing}))
    db = _db(p)
    assert db.get_meta("base:C-0003") and db.contract_status("C-0003") == "ready" and db.get_meta("base:C-0002") is None
    _set_config(p.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)   # C-0001 first, then one at a time
    seen = {}

    def note(fake, cid, role, worktree, n):
        with StateDB(home_dir(p.root) / "state.db") as db:   # the scheduler's thread: closed here
            seen[cid] = {c: db.contract_status(c) for c in ("C-0001", "C-0003")} | {"cut C-0002": db.get_meta("base:C-0002") is not None}
        return {}

    fake = FakeWorkers({("C-0003", "build", 0): note, ("C-0002", "build", 0): note})
    s, out = _run(p, fake)
    assert s.outcome == "merge_proposed", out
    assert seen["C-0003"]["C-0001"] == "integrated" and not seen["C-0003"]["cut C-0002"]   # C-0002 was ready, C-0003 held
    assert seen["C-0002"]["C-0003"] == "integrated"
    lane = home_dir(p.root) / "worktrees" / "C-0003"
    assert _builds(fake, "C-0003")[0][4] == lane == _builds(fake, "C-0002")[0][4] and _db(p).get_meta("lane:C-0002") == "C-0003"


@pytest.mark.parametrize("specs, waiting, dep, later, head", [
    pytest.param([(True, [2]), (True, []), (False, [])], "C-0001", "C-0003", "C-0002", "C-0002", id="the lane's first member waits"),
    pytest.param([(True, []), (True, [3]), (True, []), (False, [0])], "C-0002", "C-0004", "C-0003", "C-0001", id="a middle member waits"),
])
def test_a_waiting_contract_does_not_block_a_ready_one_later_in_its_lane(target_repo, monkeypatch, specs, waiting, dep, later, head):
    p = _approve_lanes(target_repo, monkeypatch, report=_report_of(*specs))
    seen = {}

    def note(fake, cid, role, worktree, n):
        with StateDB(home_dir(p.root) / "state.db") as db:   # the scheduler's thread: closed here
            seen[cid] = {c: db.contract_status(c) for c in (dep, later)}
        return {}

    fake = FakeWorkers({(waiting, "build", 0): note}, delay=0.1)
    s, out = _run(p, fake)
    assert s.outcome == "merge_proposed", out
    built = [c[0] for c in fake.calls if c[1] == "build"]
    assert built.index(later) < built.index(waiting) and seen[waiting] == {dep: "integrated", later: "integrated"}
    db, lane = _db(p), home_dir(p.root) / "worktrees" / head
    w = _builds(fake, waiting)[0]
    assert w[4] == lane and db.get_meta(f"lane:{waiting}") == head and w[5] == db.get_meta(f"base:{waiting}")
    assert (lane / "src" / LETTERS[dep] / f"{LETTERS[dep]}.py").is_file()   # its base is the integration tip, which holds dep
    assert _builds(fake, dep)[0][4] == home_dir(p.root) / "worktrees" / dep   # dep is a lane of its own: the dependency joins nothing
    members = [cid for cid, (shared, _) in zip(LETTERS, specs) if shared]
    assert all(_spans(fake, x)[1] <= _spans(fake, y)[0] or _spans(fake, y)[1] <= _spans(fake, x)[0]
               for i, x in enumerate(members) for y in members[i + 1:])   # two members of one lane never run at once
    if head != "C-0001":
        assert not (home_dir(p.root) / "worktrees" / "C-0001").exists()


def test_after_is_the_lane_member_already_in_the_worktree(target_repo, monkeypatch):
    """Lane C-0001 -> C-0002 -> C-0003, with C-0002 waiting on C-0004 (which waits on C-0001): C-0003 runs second and
    joins the worktree after C-0001, never after C-0002, which has not run."""
    import ateam.scheduler as sch
    p = _approve_lanes(target_repo, monkeypatch, report=_report_of((True, []), (True, [3]), (True, []), (False, [0])))
    real, calls = sch.contract_worktree, []

    def spy(paths, db, owner, plan_id, cid, after=None):
        calls.append((cid, after, after is None or db.get_meta(f"lane:{after}") is not None))
        return real(paths, db, owner, plan_id, cid, after=after)

    monkeypatch.setattr(sch, "contract_worktree", spy)
    s, out = _run(p, FakeWorkers())
    assert s.outcome == "merge_proposed", out
    assert {cid: after for cid, after, _ in calls} == {"C-0001": None, "C-0004": None, "C-0003": "C-0001", "C-0002": "C-0001"}
    assert all(cut for *_, cut in calls)   # `after` is always a member already in the worktree


def test_the_p0018_shape_with_a_dependency_prints_two_lanes(target_repo, monkeypatch):
    """The P-0018 shape with C-0003 depending on C-0001, files disjoint: the dependency alone no longer joins lanes."""
    _commit_on_main(target_repo, "src/ateam/cli.py", "def main():\n    pass\n")
    _commit_on_main(target_repo, "src/ateam/runner/policy.py", "POLICY = {}\n")
    report = _report3()
    for c, paths, deps in zip(report["contracts"], (["src/ateam/cli.py", "tests/test_a.py"], ["src/ateam/cli.py", "tests/test_b.py"],
                                                    ["src/ateam/runner/policy.py", "tests/test_c.py"]), ([], [], [0])):
        c["paths"], c["depends_on"] = paths, deps
    p = _approve_lanes(target_repo, monkeypatch, report=report)
    s, out = _run(p, FakeWorkers(), dry_run=True)
    assert s.outcome == "dry_run"
    assert out[1:4] == ["2 lane(s), up to 2 at once", "lane 1: C-0001 -> C-0002", "lane 2: C-0003"], out
