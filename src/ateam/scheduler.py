"""ateam run: execute an approved plan until the owner's merge gate or a pause (spec "Architecture: scheduler").

The scheduler drives the commands that exist: `ateam worker run` for BUILD and TEST sessions (subprocesses in this
process group, so a terminal Ctrl-C reaches them), `ateam verify` and `ateam contracts refresh` (subprocesses in their
own session, so they finish), and integrate's helpers to merge each verified contract onto ateam/<plan-id> at once.
Every child reports through a --result-file. Sessions run in parallel up to config.max_workers; every step that
commits to the intent repo or creates refs runs alone under a writer-preferring gate, so the tamper checks of
sessions and verifies never see another step's commit (owner, 2026-10-26). Retries follow ateam.retry; a run spends
at most config.run_budget_usd on sessions. `ateam run` never merges into the target branch.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ateam.contracts.select import drift
from ateam.contracts.store import load_contracts
from ateam.gitops import GitRepo, MergeConflict
from ateam.integrate import (IntegrateRefused, integrate, integration_branch, integration_worktree, merge_contract,
                             prepare_integration, reconcile_contracts, reconcile_feature)
from ateam.intent import IntentError, require_clean
from ateam.paths import home_dir
from ateam.plans.ops import dependency_order, plan_contract_ids, set_plan_status
from ateam.plans.store import load_plans
from ateam.records.store import load_records
from ateam.retry import StepOutcome, classify, decide
from ateam.roles import Role
from ateam.tiers import load_tiers
from ateam.worktrees import WorktreeError, contract_worktree

LIVE = ("plan_approved", "executing", "integrating")
RUNNABLE = ("ready", "failed", "verified")   # verified: merged at the next step (an interrupted run stopped before)

Launch = Callable[[Path, str, str, Path, str, "str | None", "str | None", Path], dict]


class RunRefused(Exception):
    """A precondition failed, or a re-run is needed; nothing was launched by this call."""


@dataclass
class RunSummary:
    plan_id: str
    outcome: Literal["merge_proposed", "paused", "stalled", "interrupted", "rerun", "dry_run"]
    contracts: dict[str, str]
    spent_usd: float
    attempts: int
    messages: list[str] = field(default_factory=list)
    proposal: Path | None = None


class Gate:
    """Readers (sessions) share; a writer (a step that commits or creates refs) runs alone. Writer-preferring: while a
    writer waits, no new reader starts, so commits are not starved by a stream of sessions."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._waiting:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._cond:
            self._waiting += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


class LockedEcho:
    def __init__(self, echo: Callable[[str], None]) -> None:
        self._echo, self._lock = echo, threading.Lock()

    def __call__(self, message: str) -> None:
        with self._lock:
            self._echo(message)


class Budget:
    """Spend of this run, with each launched session's tier cap reserved until it reports its cost: the run never
    launches a session whose cap would take it past the limit."""

    def __init__(self, limit: float) -> None:
        self.limit, self.spent, self._reserved, self._lock = limit, 0.0, 0.0, threading.Lock()

    def reserve(self, usd: float) -> bool:
        with self._lock:
            if self.spent + self._reserved + usd > self.limit + 1e-9:
                return False
            self._reserved += usd
            return True

    def settle(self, reserved: float, actual: float) -> None:
        with self._lock:
            self._reserved -= reserved
            self.spent += actual


def ateam_argv() -> list[str]:
    exe = Path(sys.executable).with_name("ateam")
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "ateam.cli"]


def _read_result(path: Path, code: int, missing: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"code": code, **missing, "error": f"no result file (exit {code})"}


def default_launch(target: Path, cid: str, role: str, worktree: Path, base: str, tier: str | None, prompt: str | None,
                   result_file: Path) -> dict:
    """`ateam worker run` as a subprocess in this process group; its output goes to a log next to the result file."""
    argv = [*ateam_argv(), "worker", "run", cid, "--role", role, "--cwd", str(worktree), "--base", base,
            "--result-file", str(result_file), "--target", str(target)]
    if tier:
        argv += ["--tier", tier]
    if prompt:
        argv += ["--prompt", prompt]
    log = result_file.with_suffix(".log")
    with log.open("w") as out:
        proc = subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    return {"log": str(log), **_read_result(result_file, proc.returncode, {"outcome": "failed", "subtype": None})}


def run_child(argv: list[str], result_file: Path) -> dict:
    """verify or refresh, in its own process session so a terminal Ctrl-C lets it finish."""
    log = result_file.with_suffix(".log")
    with log.open("w") as out:
        proc = subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    missing = {"status": "failed", "failure_class": "environment", "reasons": [], "promoted": [], "drifted": []}
    return {"log": str(log), **_read_result(result_file, proc.returncode, missing)}


@dataclass
class _Outcome:
    cid: str
    kind: Literal["done", "pause", "interrupted", "halted", "error"]
    reason: str | None = None
    detail: str | None = None
    exc: BaseException | None = None


def run_plan(ctx_factory: Callable[[], object], plan_id: str, echo: Callable[[str], None], stop: threading.Event,
             launch: Launch = default_launch, dry_run: bool = False,
             child: Callable[[list[str], Path], dict] = run_child) -> RunSummary:
    ctx = ctx_factory()
    say = LockedEcho(echo)
    acquired = False
    if not dry_run:
        if not ctx.db.acquire_run_lock(plan_id):
            info = ctx.db.run_lock_info() or {}
            raise RunRefused(f"the run lock is held by {info.get('plan_id')} (pid {info.get('pid')}, since {info.get('acquired_at')}); "
                             "if that process is gone, remove the lock by hand")
        acquired = True
    try:
        plan, cs, cids = _preflight(ctx, plan_id, say, dry_run)
        if dry_run:
            return _dry_run(ctx, plan_id, cs, cids, say)
        return _Run(ctx_factory, ctx, plan_id, cids, say, stop, launch, child).execute()
    finally:
        if acquired:
            ctx.db.release_run_lock(plan_id)


def _preflight(ctx, plan_id: str, say, dry_run: bool):
    db = ctx.db
    plan = load_plans(ctx.paths.plans).get(plan_id)
    if plan is None:
        raise RunRefused(f"unknown plan {plan_id}")
    stored = db.feature_status(plan_id)
    if "paused" in (stored, str(plan.status)):
        last = (db.transitions("feature", plan_id) or [{}])[-1].get("reason") or "no reason recorded"
        raise RunRefused(f"{plan_id} is paused ({last}); resuming a paused plan is not built yet")
    if stored not in LIVE and str(plan.status) not in LIVE:
        raise RunRefused(f"{plan_id} is {plan.status}; ateam run needs an approved plan (merge a proposal with ateam merge)")
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        raise RunRefused(str(e)) from None
    if not dry_run:
        try:
            stored = reconcile_feature(ctx.paths, db, plan, say)
        except IntegrateRefused as e:
            raise RunRefused(str(e)) from None
        if stored not in LIVE:
            raise RunRefused(f"{plan_id} is {stored}; ateam run needs an approved plan")
    cs = load_contracts(ctx.paths.contracts)
    records = load_records(ctx.paths.records)
    cids = plan_contract_ids(db, plan)
    if not cids:
        raise RunRefused(f"{plan_id} has no contracts")
    for cid in cids:
        c = cs.get(cid)
        if c is None:
            raise RunRefused(f"{cid} has no contract file")
        frozen = db.contract_hash(cid)
        if frozen is None:
            raise RunRefused(f"{cid} is not frozen")
        if c.content_hash() != frozen:
            raise RunRefused(f"{cid} differs from its frozen hash (hand edit after freeze)")
        drifted = drift(c, records)
        if drifted:
            raise RunRefused(f"{cid} has record drift ({', '.join(drifted)}); re-freeze or replan first")
        if db.contract_status(cid) is None:
            raise RunRefused(f"{cid} has no row in the state store")
        if not c.checks:
            raise RunRefused(f"{cid} has no deterministic checks of its own; verify would refuse it")
    if stored == "integrating":
        unfinished = [f"{cid} ({db.contract_status(cid)})" for cid in cids if db.contract_status(cid) not in ("verified", "integrated")]
        if unfinished:
            raise RunRefused(f"{plan_id} is integrating but {', '.join(unfinished)} is unfinished; investigate")
    return plan, cs, cids


def _dry_run(ctx, plan_id: str, cs, cids: list[str], say) -> RunSummary:
    tiers = load_tiers(ctx.config.tiers)
    level: dict[str, int] = {}

    def depth(cid: str) -> int:
        if cid not in level:
            level[cid] = 1 + max((depth(d) for d in cs[cid].depends_on if d in cids), default=0)
        return level[cid]

    waves: dict[int, list[str]] = {}
    for cid in sorted(cids):
        waves.setdefault(depth(cid), []).append(cid)
    say(f"dry run {plan_id}: {len(cids)} contract(s), up to {ctx.config.max_workers} at once; nothing is launched or written")
    for n in sorted(waves):
        say(f"wave {n}: {', '.join(waves[n])}")
    total = 0.0
    for cid in dependency_order({c: cs[c] for c in cids}):
        c = cs[cid]
        b, t = c.allocation.tier["build"], c.allocation.tier["test"]
        bs, ts = tiers[b][Role.build], tiers[t][Role.test]
        worst = c.budget.attempts * (bs.usd + ts.usd)
        total += worst
        say(f"  {cid}: build {b} ({bs.model}), test {t} ({ts.model}), up to {c.budget.attempts} attempt(s), worst ${worst:.2f}")
    say(f"worst case ${total:.2f}; run budget ${ctx.config.run_budget_usd:.2f}")
    return RunSummary(plan_id, "dry_run", {cid: str(cs[cid].status) for cid in cids}, 0.0, 0)


class _Run:
    def __init__(self, ctx_factory, ctx, plan_id: str, cids: list[str], say, stop: threading.Event, launch: Launch, child) -> None:
        self.ctx_factory, self.ctx, self.plan_id, self.cids = ctx_factory, ctx, plan_id, cids
        self.say, self.stop, self.launch, self.child = say, stop, launch, child
        self.gate, self.halt, self.results = Gate(), threading.Event(), queue.Queue()
        self.budget = Budget(ctx.config.run_budget_usd)
        self.tiers = load_tiers(ctx.config.tiers)
        self.sessions = 0
        self._counter, self._counter_lock = 0, threading.Lock()
        self.runs_dir = home_dir(ctx.paths.root) / "runs" / plan_id
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # -- the main loop -------------------------------------------------------

    def execute(self) -> RunSummary:
        ctx, db = self.ctx, self.ctx.db
        self._close_open_attempts()
        with self.gate.exclusive():
            plan = load_plans(ctx.paths.plans)[self.plan_id]
            if db.feature_status(self.plan_id) == "plan_approved":
                set_plan_status(ctx.paths, db, plan, ["executing"], by="run")
                self.say(f"{self.plan_id} -> executing")
            try:
                prepare_integration(ctx, self.plan_id, ctx.git.rev_parse(ctx.config.target_branch), self.say)
                reconcile_contracts(db, ctx.git, load_contracts(ctx.paths.contracts), self.cids, integration_branch(self.plan_id), self.say)
            except IntegrateRefused as e:
                raise RunRefused(str(e)) from None
            self._refresh(ctx)

        running: dict[str, threading.Thread] = {}
        settled: set[str] = set()
        pauses: list[_Outcome] = []
        errors: list[_Outcome] = []
        cs = load_contracts(ctx.paths.contracts)
        order = dependency_order({c: cs[c] for c in self.cids})
        while True:
            try:
                while True:
                    o = self.results.get(timeout=0.05 if running else 0)
                    running.pop(o.cid).join()
                    settled.add(o.cid)
                    if o.kind == "pause":
                        pauses.append(o)
                    elif o.kind == "error":
                        errors.append(o)
            except queue.Empty:
                pass
            if errors or pauses or self.stop.is_set():
                self.halt.set()
            if not self.halt.is_set():
                free = ctx.config.max_workers - len(running)
                for cid in [c for c in order if c not in running and c not in settled and db.contract_status(c) in RUNNABLE][:max(free, 0)]:
                    t = threading.Thread(target=self._pipeline_thread, args=(cid,), name=f"ateam-run-{cid}", daemon=True)
                    running[cid] = t
                    t.start()
            if not running:
                break

        statuses = {cid: db.contract_status(cid) for cid in self.cids}
        if errors:
            self._close_open_attempts()
            raise errors[0].exc
        if self.stop.is_set():
            self._close_open_attempts()
            self.say(f"{self.plan_id}: interrupted; worktrees are kept and a re-run continues")
            return self._summary("interrupted", statuses, ["interrupted"])
        if pauses:
            first = pauses[0]
            why = f"{first.reason}: {first.detail}"
            with self.gate.exclusive():
                set_plan_status(ctx.paths, db, load_plans(ctx.paths.plans)[self.plan_id], ["paused"], by="run", why=why)
            self.say(f"{self.plan_id} -> paused ({why})")
            return self._summary("paused", statuses, [why])
        if all(s == "integrated" for s in statuses.values()):
            try:
                res = integrate(ctx, self.plan_id, self.say)
            except IntegrateRefused as e:
                return self._summary("rerun", statuses, [str(e)])
            outcome = "merge_proposed" if res.outcome == "merge_proposed" else "paused"
            return self._summary(outcome, {cid: db.contract_status(cid) for cid in self.cids}, res.messages, res.proposal)
        blockers = [f"{cid} ({s})" for cid, s in statuses.items() if s != "integrated"]
        message = "nothing more can run; unfinished: " + ", ".join(blockers)
        self.say(f"{self.plan_id}: stalled; {message}")
        return self._summary("stalled", statuses, [message])

    def _summary(self, outcome, statuses, messages, proposal=None) -> RunSummary:
        self.say(f"summary: {outcome}; {self.sessions} session(s), ${self.budget.spent:.2f} spent; "
                 + ", ".join(f"{c} {s}" for c, s in statuses.items()))
        return RunSummary(self.plan_id, outcome, statuses, self.budget.spent, self.sessions, messages, proposal)

    def _close_open_attempts(self) -> None:
        for a in self.ctx.db.open_attempts(self.cids):
            self.ctx.db.interrupt_attempt(a["attempt_id"])
            self.say(f"{a['contract_id']} {a['role']} attempt {a['attempt_id']} closed as interrupted")

    # -- one contract ---------------------------------------------------------

    def _pipeline_thread(self, cid: str) -> None:
        try:
            o = self._pipeline(cid)
        except BaseException as e:  # noqa: BLE001  (reported to the main loop, which re-raises)
            self.halt.set()
            o = _Outcome(cid, "error", exc=e)
        if o.kind == "pause":
            self.halt.set()
        self.results.put(o)

    def _pipeline(self, cid: str) -> _Outcome:
        ctx = self.ctx_factory()
        db = ctx.db
        if db.contract_status(cid) == "verified":
            return self._merge(ctx, cid)
        c = load_contracts(ctx.paths.contracts)[cid]
        with self.gate.exclusive():
            try:
                w = contract_worktree(ctx.paths, db, ctx.git, self.plan_id, cid)
            except WorktreeError as e:
                return _Outcome(cid, "pause", "environment_failure", f"{cid}: {e}")
        frozen = c.allocation.tier["build"]
        builds = db.attempts_for(cid, role="build")
        tier = builds[-1]["tier"] if builds and builds[-1]["tier"] in self.tiers else frozen
        escalated = any(a["tier"] != frozen for a in builds)
        allowed = c.budget.attempts
        prompt: str | None = None
        while True:
            used = len(db.attempts_for(cid, role="build"))
            if self.halt.is_set():
                return _Outcome(cid, "halted")
            if used >= allowed:
                return _Outcome(cid, "pause", "budget_exhausted", f"{cid} has used all {allowed} BUILD attempts")
            r = self._session(ctx, cid, "build", w, tier if tier != frozen else None, prompt, self.tiers[tier][Role.build].usd)
            if r is None:
                return self._budget_pause(cid, "build")
            if r.get("outcome") == "interrupted" or self.stop.is_set():
                return _Outcome(cid, "interrupted")
            used += 1
            o = StepOutcome("build", int(r.get("code", 1)), r.get("outcome") or "failed", subtype=r.get("subtype"))
            d = decide(classify(o), used, allowed, tier, escalated, o.subtype)
            if d.action == "pause":
                return _Outcome(cid, "pause", d.reason, f"{cid} build {o.outcome}" + (f" ({r['error']})" if r.get("error") else ""))
            if d.action == "retry":
                escalated, tier = escalated or d.tier != tier, d.tier
                prompt = _retry_prompt(c, f"the BUILD attempt ended {o.outcome}" + (f": {r['error']}" if r.get("error") else ""))
                continue

            if self.halt.is_set():
                return _Outcome(cid, "halted")
            test_tier = c.allocation.tier["test"]
            r = self._session(ctx, cid, "test", w, None, None, self.tiers[test_tier][Role.test].usd)
            if r is None:
                return self._budget_pause(cid, "test")
            if r.get("outcome") == "interrupted" or self.stop.is_set():
                return _Outcome(cid, "interrupted")
            o = StepOutcome("test", int(r.get("code", 1)), r.get("outcome") or "failed", subtype=r.get("subtype"),
                            test_class=r.get("test_class"))
            d = decide(classify(o), used, allowed, tier, escalated, None)
            if d.action == "pause":
                return _Outcome(cid, "pause", d.reason, f"{cid} test {r.get('test_outcome') or o.outcome}")
            if d.action == "retry":
                prompt = _retry_prompt(c, "the TEST worker judged these criteria not met:\n" + _unmet(r.get("report")))
                continue

            if self.halt.is_set():
                return _Outcome(cid, "halted")
            with self.gate.exclusive():
                rf = self._result_path(cid, "verify")
                v = self.child([*ateam_argv(), "verify", cid, "--cwd", str(w.path), "--base", w.base, "--result-file", str(rf),
                                "--target", str(ctx.paths.root)], rf)
            self.say(f"{cid} verify {v.get('status')}" + (f" ({v['failure_class']})" if v.get("failure_class") else ""))
            o = StepOutcome("verify", int(v.get("code", 1)), v.get("status") or "failed", verify_status=v.get("status"),
                            failure_class=v.get("failure_class"))
            d = decide(classify(o), used, allowed, tier, escalated, None)
            if d.action == "pause":
                return _Outcome(cid, "pause", d.reason, f"{cid} verify {v.get('status')}: " + "; ".join(v.get("reasons") or []))
            if d.action == "retry":
                prompt = _retry_prompt(c, "verify failed: " + "; ".join(v.get("reasons") or ["no reason given"]))
                continue
            return self._merge(ctx, cid)

    def _session(self, ctx, cid: str, role: str, w, tier: str | None, prompt: str | None, usd: float) -> dict | None:
        if not self.budget.reserve(usd):
            self.halt.set()
            return None
        actual = usd
        try:
            with self.gate.shared():
                self.sessions += 1
                r = self.launch(ctx.paths.root, cid, role, w.path, w.base, tier, prompt, self._result_path(cid, role))
            actual = float(r.get("cost_usd") or 0.0)
        finally:
            self.budget.settle(usd, actual)
        self.say(f"{cid} {role} {r.get('outcome')} ${actual:.2f}" + (f" tier {tier}" if tier else "")
                 + (f" (log {r['log']})" if r.get("code") not in (0, None) and r.get("log") else ""))
        return r

    def _budget_pause(self, cid: str, role: str) -> _Outcome:
        return _Outcome(cid, "pause", "budget_exhausted",
                        f"run budget ${self.budget.limit:.2f} would be exceeded by {cid} {role} (spent ${self.budget.spent:.2f})")

    def _merge(self, ctx, cid: str) -> _Outcome:
        with self.gate.exclusive():
            wt = GitRepo(integration_worktree(ctx.paths, self.plan_id))
            try:
                merge_contract(ctx, wt, self.plan_id, cid, self.say)
            except MergeConflict as e:
                return _Outcome(cid, "pause", "environment_failure", f"{cid} conflicts in {', '.join(e.paths)}")
            self._refresh(ctx)
        return _Outcome(cid, "done")

    def _refresh(self, ctx) -> None:
        rf = self._result_path("refresh", "contracts")
        r = self.child([*ateam_argv(), "contracts", "refresh", self.plan_id, "--result-file", str(rf), "--target", str(ctx.paths.root)], rf)
        for cid in r.get("promoted") or []:
            self.say(f"{cid} -> ready")
        for cid in r.get("drifted") or []:
            self.say(f"{cid}: record drift; left draft")

    def _result_path(self, cid: str, role: str) -> Path:
        with self._counter_lock:
            self._counter += 1
            return self.runs_dir / f"{self._counter:04d}-{cid}-{role}.json"


def _unmet(report: dict | None) -> str:
    verdicts = (report or {}).get("verdicts") or []
    lines = [f"- {v.get('id')}: {v.get('evidence') or v.get('verdict')}" for v in verdicts if v.get("verdict") != "met"]
    return "\n".join(lines) or "- (no evidence reported)"


def _retry_prompt(contract, why: str) -> str:
    return (f"Implement contract {contract.id}: {contract.title}. Follow the brief in your system prompt and end with the "
            f"structured report. The previous attempt did not pass: {why}")
