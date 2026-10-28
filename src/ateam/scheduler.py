"""ateam run: execute an approved plan until the owner's merge gate or a pause (spec "Architecture: scheduler").

The scheduler drives the commands that exist: `ateam worker run` for BUILD and TEST sessions (subprocesses in this
process group, so a terminal Ctrl-C reaches them), `ateam verify` and `ateam contracts refresh` (subprocesses in their
own session, so they finish), and integrate's helpers to merge each verified contract onto ateam/<plan-id> at once.
Every child reports through a --result-file. Sessions run in parallel up to config.max_workers; every step that
commits to the intent repo or creates refs runs alone under a writer-preferring gate, so the tamper checks of
sessions and verifies never see another step's commit (owner, 2026-10-26). Retries follow ateam.retry; a run spends
at most config.run_budget_usd on sessions. `ateam run` never merges into the target branch.

Contracts whose declared paths overlap, or that depend on each other, form a lane (design 2026-10-27): a lane runs
its contracts one after another in one shared worktree, each starting from the previous contract's verified head once
that contract is integrated, and lanes run in parallel up to config.max_workers. A verified contract whose merge
conflicts gets one rebase BUILD in its own worktree, then TEST and verify again (owner, 2026-10-27).
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ateam.contracts.select import drift
from ateam.contracts.store import load_contracts
from ateam.gitops import GitError, GitRepo, MergeConflict
from ateam.globs import matches
from ateam.integrate import (IntegrateRefused, integration_branch, integration_worktree, merge_contract, prepare_integration,
                             reconcile_contracts, reconcile_feature, reopen_for_rebase)
from ateam.intent import WRITER_KEY, IntentError, intent_git, record_coordinator_commits, require_clean
from ateam.paths import home_dir
from ateam.plans.ops import dependency_order, plan_contract_ids, set_plan_status
from ateam.plans.store import load_plans
from ateam.records.store import load_records
from ateam.retry import StepOutcome, classify, decide, decide_conflict, next_tier
from ateam.roles import Role
from ateam.tiers import at_least, load_tiers
from ateam.worktrees import WorktreeError, contract_worktree, prepare_rebase, rebased_this_window, worktree_path

LIVE = ("plan_approved", "executing", "integrating")
STOP_POLL_S = 0.5   # how often the main loop reads a stop request from the store
RUNNABLE = ("ready", "failed", "verified")   # verified: merged at the next step (an interrupted run stopped before)

Launch = Callable[[Path, str, str, Path, str, "str | None", "str | None", Path], dict]
Investigate = Callable[[Path, str, str, Path, Path], dict]   # (target, plan_id, cid, worktree, result_file) -> result


class RunRefused(Exception):
    """A precondition failed, or a re-run is needed; nothing was launched by this call."""


@dataclass
class RunSummary:
    plan_id: str
    outcome: Literal["merge_proposed", "paused", "stalled", "budget", "interrupted", "rerun", "dry_run"]
    contracts: dict[str, str]
    spent_usd: float
    attempts: int
    messages: list[str] = field(default_factory=list)
    proposal: Path | None = None


class Gate:
    """Three modes. Readers (sessions) share. A writer (a step that commits to the intent repo or merges: refresh,
    merge-at-verify) runs beside sessions but alone among writers; a running session's tamper check accepts the
    intent commits it records (overlap gate design, owner 2026-10-28). Exclusive (verify, run start and end) runs
    alone. Exclusive-preferring: while it waits, no new reader or writer starts, so it is not starved."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writing = False     # a writer is in
        self._writer = False      # exclusive is in
        self._waiting = 0         # exclusive steps waiting

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
    def writer(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._writing or self._waiting:
                self._cond.wait()
            self._writing = True
        try:
            yield
        finally:
            with self._cond:
                self._writing = False
                self._cond.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._cond:
            self._waiting += 1
            while self._writer or self._writing or self._readers:
                self._cond.wait()
            self._waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


@contextmanager
def _no_lock() -> Iterator[None]:
    yield


class LockedEcho:
    def __init__(self, echo: Callable[[str], None]) -> None:
        self._echo, self._lock = echo, threading.Lock()

    def __call__(self, message: str) -> None:
        with self._lock:
            self._echo(message)


class Budget:
    """Spend of this run, with each launched session's tier cap reserved until it reports its cost: the run never
    launches a session whose cap would take it past the limit. A reservation that does not fit waits while other
    sessions are in flight (their settled cost is usually far below their cap) and gives up only when nothing else
    is reserved."""

    def __init__(self, limit: float) -> None:
        self.limit, self.spent, self._reserved = limit, 0.0, 0.0
        self._cond = threading.Condition()

    def reserve(self, usd: float, halt: threading.Event) -> Literal["ok", "exhausted", "halted"]:
        with self._cond:
            while True:
                if halt.is_set():
                    return "halted"
                if self.spent + self._reserved + usd <= self.limit + 1e-9:
                    self._reserved += usd
                    return "ok"
                if self._reserved <= 1e-9:
                    return "exhausted"
                self._cond.wait(0.1)

    def settle(self, reserved: float, actual: float) -> None:
        with self._cond:
            self._reserved -= reserved
            self.spent += actual
            self._cond.notify_all()


def fresh_dir(parent: Path, name: str) -> Path:
    """A new directory under `parent`: `name`, or `name-2`, `name-3`... when two runs start in the same second."""
    parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        path = parent / (name if n == 1 else f"{name}-{n}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"no free run directory for {name} under {parent}")


def ateam_argv() -> list[str]:
    exe = Path(sys.executable).with_name("ateam")
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "ateam.cli"]


def _read_result(path: Path, code: int, missing: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"code": code, **missing, "error": f"no result file (exit {code})"}


class _Children:
    """The session processes this scheduler launched. An owner stop interrupts them by pid; ATeam never signals a
    process group it did not create (owner, 2026-10-26). verify, refresh and integrate are not registered: they run
    in their own sessions and finish."""

    _procs: set[subprocess.Popen] = set()
    _lock = threading.Lock()

    @classmethod
    def add(cls, proc: subprocess.Popen) -> None:
        with cls._lock:
            cls._procs.add(proc)

    @classmethod
    def discard(cls, proc: subprocess.Popen) -> None:
        with cls._lock:
            cls._procs.discard(proc)

    @classmethod
    def interrupt_all(cls) -> int:
        """SIGINT to every registered child still running; the number signalled."""
        with cls._lock:
            live = [p for p in cls._procs if p.poll() is None]
        for proc in live:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        return len(live)


def _run_registered(argv: list[str], log: Path) -> int:
    with log.open("w") as out:
        proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        _Children.add(proc)
        try:
            return proc.wait()
        finally:
            _Children.discard(proc)


def default_launch(target: Path, cid: str, role: str, worktree: Path, base: str, tier: str | None, prompt: str | None,
                   result_file: Path) -> dict:
    """`ateam worker run` as a subprocess in this process group (a terminal Ctrl-C reaches it), registered so an
    owner stop can interrupt it; its output goes to a log next to the result file."""
    argv = [*ateam_argv(), "worker", "run", cid, "--role", role, "--cwd", str(worktree), "--base", base,
            "--result-file", str(result_file), "--target", str(target)]
    if tier:
        argv += ["--tier", tier]
    if prompt:
        argv += ["--prompt", prompt]
    log = result_file.with_suffix(".log")
    code = _run_registered(argv, log)
    return {"log": str(log), **_read_result(result_file, code, {"outcome": "failed", "subtype": None})}


def default_investigate(target: Path, plan_id: str, cid: str, worktree: Path, result_file: Path) -> dict:
    """`ateam worker pause-investigate` as a registered child, like a session: Ctrl-C and an owner stop reach it."""
    argv = [*ateam_argv(), "worker", "pause-investigate", plan_id, cid, "--cwd", str(worktree), "--result-file", str(result_file),
            "--target", str(target)]
    log = result_file.with_suffix(".log")
    code = _run_registered(argv, log)
    return {"log": str(log), **_read_result(result_file, code, {"outcome": "failed", "cost_usd": None})}


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
    kind: Literal["done", "pause", "budget", "interrupted", "halted", "error", "rebase"]   # rebase: _merge to _pipeline only
    reason: str | None = None
    detail: str | None = None
    exc: BaseException | None = None
    evidence: dict | None = None     # the result of the step that paused, kept on the pause record
    conflict: bool = False           # a merge conflict: resume leads to the rebase BUILD


def run_plan(ctx_factory: Callable[[], object], plan_id: str, echo: Callable[[str], None], stop: threading.Event,
             launch: Launch | None = None, dry_run: bool = False,
             child: Callable[[list[str], Path], dict] = run_child, investigate: Investigate | None = None) -> RunSummary:
    launch = launch if launch is not None else default_launch   # resolved now, so a test's replacement is honoured
    investigate = investigate if investigate is not None else default_investigate
    ctx = ctx_factory()
    try:
        say = LockedEcho(echo)
        acquired = False
        if not dry_run:
            if not ctx.db.acquire_run_lock(plan_id):
                info = ctx.db.run_lock_info() or {}
                raise RunRefused(f"the run lock is held by {info.get('plan_id')} (pid {info.get('pid')}, since {info.get('acquired_at')}); "
                                 "if that process is gone, remove the lock by hand")
            acquired = True
            ctx.db.clear_stop(plan_id)   # a request left from before this run is not this run's
        try:
            if not dry_run:
                from ateam.exclusion import live_planning
                for planning_id, planning_pid in live_planning(ctx.db):
                    raise RunRefused(f"{planning_id} is being planned (pid {planning_pid}); wait for it to finish")
            plan, cs, cids = _preflight(ctx, plan_id, say, dry_run)
            if dry_run:
                return _dry_run(ctx, plan_id, cs, cids, say)
            return _Run(ctx_factory, ctx, plan_id, cids, say, stop, launch, child, investigate).execute()
        finally:
            if acquired:
                ctx.db.release_run_lock(plan_id)
    finally:
        ctx.close()   # after the lock is released, in the thread that made it


def _preflight(ctx, plan_id: str, say, dry_run: bool):
    db = ctx.db
    plan = load_plans(ctx.paths.plans).get(plan_id)
    if plan is None:
        raise RunRefused(f"unknown plan {plan_id}")
    stored = db.feature_status(plan_id)
    if "paused" in (stored, str(plan.status)):
        last = (db.transitions("feature", plan_id) or [{}])[-1].get("reason") or "no reason recorded"
        raise RunRefused(f"{plan_id} is paused ({last}); resume it with: ateam resume {plan_id}")
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
    problems = binding_problems(ctx, cids, cs, records)
    if problems:
        raise RunRefused(problems[0])
    if stored == "integrating":
        unfinished = [f"{cid} ({db.contract_status(cid)})" for cid in cids if db.contract_status(cid) not in ("verified", "integrated")
                      and not (db.contract_status(cid) == "building" and db.get_meta(f"rebase:{cid}"))]   # a rebase under way
        if unfinished:
            raise RunRefused(f"{plan_id} is integrating but {', '.join(unfinished)} is unfinished; investigate")
    return plan, cs, cids


def binding_problems(ctx, cids: list[str], cs: dict | None = None, records: dict | None = None) -> list[str]:
    """Why each of these contracts cannot run as frozen; empty when every one binds. `ateam run` refuses on the first,
    `ateam resume` names them all."""
    cs = load_contracts(ctx.paths.contracts) if cs is None else cs
    records = load_records(ctx.paths.records) if records is None else records
    db, out = ctx.db, []
    for cid in cids:
        c = cs.get(cid)
        if c is None:
            out.append(f"{cid} has no contract file"); continue
        frozen = db.contract_hash(cid)
        if frozen is None:
            out.append(f"{cid} is not frozen"); continue
        if c.content_hash() != frozen:
            out.append(f"{cid} differs from its frozen hash (hand edit after freeze)"); continue
        drifted = drift(c, records)
        if drifted:
            out.append(f"{cid} has record drift ({', '.join(drifted)}); re-freeze or replan first"); continue
        if db.contract_status(cid) is None:
            out.append(f"{cid} has no row in the state store"); continue
        if not c.checks:
            out.append(f"{cid} has no deterministic checks of its own; verify would refuse it")
    return out


def lanes(contracts: dict, cids: list[str], files: list[str]) -> list[list[str]]:
    """The plan's contracts grouped into lanes (design 2026-10-27, decision 1): two contracts share a lane when a file
    matches a glob of each, when a glob of one names the other's glob taken as a path (a file not created yet), or
    when one depends on the other; a lane is the connected group. Each lane is in dependency order, ties by id, and
    lanes are ordered by their first contract."""
    globs = {cid: [*contracts[cid].paths, *contracts[cid].incidental] for cid in cids}
    hits = {cid: {f for f in files if any(matches(g, f) for g in globs[cid])} for cid in cids}
    parent = {cid: cid for cid in cids}

    def root(cid: str) -> str:
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid

    def overlap(a: str, b: str) -> bool:
        if b in contracts[a].depends_on or a in contracts[b].depends_on or hits[a] & hits[b]:
            return True
        return any(ga == gb or matches(ga, gb) or matches(gb, ga) for ga in globs[a] for gb in globs[b])

    for i, a in enumerate(cids):
        for b in cids[i + 1:]:
            if root(a) != root(b) and overlap(a, b):
                parent[root(a)] = root(b)
    groups: dict[str, list[str]] = {}
    for cid in cids:
        groups.setdefault(root(cid), []).append(cid)
    return sorted((dependency_order({c: contracts[c] for c in g}) for g in groups.values()), key=lambda lane: lane[0])


def plan_lanes(ctx, plan_id: str, cs: dict, cids: list[str]) -> list[list[str]]:
    """The plan's lanes, from the files at its recorded integration start (the target tip before a run started)."""
    raw = ctx.db.get_meta(f"integration:{plan_id}")
    rev = json.loads(raw)["start"] if raw else ctx.config.target_branch
    return lanes(cs, cids, ctx.git.files_at(rev))


def lane_lines(groups: list[list[str]], max_workers: int) -> list[str]:
    return [f"{len(groups)} lane(s), up to {max_workers} at once",
            *(f"lane {n}: {' -> '.join(lane)}" for n, lane in enumerate(groups, 1))]


def _dry_run(ctx, plan_id: str, cs, cids: list[str], say) -> RunSummary:
    tiers = load_tiers(ctx.config.tiers)
    say(f"dry run {plan_id}: {len(cids)} contract(s); nothing is launched or written")
    for line in lane_lines(plan_lanes(ctx, plan_id, cs, cids), ctx.config.max_workers):
        say(line)
    total = 0.0
    for cid in dependency_order({c: cs[c] for c in cids}):
        c = cs[cid]
        b, t = c.allocation.tier["build"], c.allocation.tier["test"]
        bs, ts = tiers[b][Role.build], tiers[t][Role.test]
        up = next_tier(b) or b                       # worst case: one escalation after the first attempt
        later = tiers[up][Role.build].usd + tiers[at_least(up, t)][Role.test].usd
        worst = (bs.usd + ts.usd) + (c.budget.attempts - 1) * later
        total += worst
        say(f"  {cid}: build {b} ({bs.model}), test {t} ({ts.model}), up to {c.budget.attempts} attempt(s), worst ${worst:.2f}")
    say(f"worst case ${total:.2f}; run budget ${ctx.config.run_budget_usd:.2f}")
    return RunSummary(plan_id, "dry_run", {cid: str(cs[cid].status) for cid in cids}, 0.0, 0)


class _Run:
    def __init__(self, ctx_factory, ctx, plan_id: str, cids: list[str], say, stop: threading.Event, launch: Launch, child,
                 investigate: Investigate) -> None:
        self.ctx_factory, self.ctx, self.plan_id, self.cids = ctx_factory, ctx, plan_id, cids
        self.say, self.stop, self.launch, self.child, self.investigate = say, stop, launch, child, investigate
        self.gate, self.halt, self.results = Gate(), threading.Event(), queue.Queue()
        self.budget = Budget(ctx.config.run_budget_usd)
        self.tiers = load_tiers(ctx.config.tiers)
        self.sessions = 0
        self._counter, self._counter_lock = 0, threading.Lock()
        self._refs = threading.Lock()   # ATeam's own git ref and worktree creation, one at a time; taken after the Gate
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.runs_dir = fresh_dir(home_dir(ctx.paths.root) / "runs" / plan_id, f"{stamp}-{os.getpid()}")   # never reused
        self.lanes: list[list[str]] = []
        self.prev: dict[str, str | None] = {}

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
            self.lanes = plan_lanes(ctx, self.plan_id, load_contracts(ctx.paths.contracts), self.cids)
            self.prev = {cid: (lane[i - 1] if i else None) for lane in self.lanes for i, cid in enumerate(lane)}

        running: dict[str, threading.Thread] = {}
        settled: set[str] = set()
        pauses: list[_Outcome] = []
        errors: list[_Outcome] = []
        budget_stops: list[_Outcome] = []
        cs = load_contracts(ctx.paths.contracts)
        order = dependency_order({c: cs[c] for c in self.cids})
        polled = 0.0
        while True:
            if not self.stop.is_set() and time.monotonic() - polled >= STOP_POLL_S:
                polled = time.monotonic()
                if db.stop_requested(self.plan_id):   # `ateam stop` from another terminal
                    self.say(f"{self.plan_id}: stop requested by the owner; interrupting running sessions")
                    self.stop.set()
                    _Children.interrupt_all()
            try:
                while True:
                    o = self.results.get(timeout=0.05 if running else 0)
                    running.pop(o.cid).join()
                    settled.add(o.cid)
                    if o.kind == "pause":
                        pauses.append(o)
                    elif o.kind == "error":
                        errors.append(o)
                    elif o.kind == "budget":
                        budget_stops.append(o)
            except queue.Empty:
                pass
            if errors or pauses or budget_stops or self.stop.is_set():
                self.halt.set()
            if not self.halt.is_set():
                free = ctx.config.max_workers - len(running)
                for cid in [c for c in order if c not in running and c not in settled and self._startable(db, cs, c)][:max(free, 0)]:
                    t = threading.Thread(target=self._pipeline_thread, args=(cid,), name=f"ateam-run-{cid}", daemon=True)
                    running[cid] = t
                    t.start()
            if not running:
                break

        statuses = {cid: db.contract_status(cid) for cid in self.cids}
        if errors:
            self._close_open_attempts()
            raise errors[0].exc
        if self.stop.is_set() and db.stop_requested(self.plan_id) and not pauses:
            self._close_open_attempts()
            why = "owner_stopped: stopped by the owner"
            with self.gate.exclusive():
                set_plan_status(ctx.paths, db, load_plans(ctx.paths.plans)[self.plan_id], ["paused"], by="owner", why=why)
                db.record_pause(self.plan_id, "owner_stopped", "stopped by the owner", [])
                db.clear_stop(self.plan_id)
            self.say(f"{self.plan_id} -> paused ({why})")
            return self._summary("paused", {cid: db.contract_status(cid) for cid in self.cids}, [why])
        if self.stop.is_set() and not pauses:
            self._close_open_attempts()
            self.say(f"{self.plan_id}: interrupted; worktrees are kept and a re-run continues")
            return self._summary("interrupted", statuses, ["interrupted"])
        if budget_stops and not pauses:
            message = budget_stops[0].detail
            self.say(f"{self.plan_id}: stopped at the run budget ({message}); the plan stays executing; "
                     "re-run with a larger run_budget_usd to continue")
            return self._summary("budget", statuses, [message])
        if pauses:
            for o in pauses:
                o.detail = (o.detail or "") + self._blocks(db, o.cid)
            first = pauses[0]
            why = f"{first.reason}: {first.detail}"
            self._close_open_attempts()
            with self.gate.exclusive():
                set_plan_status(ctx.paths, db, load_plans(ctx.paths.plans)[self.plan_id], ["paused"], by="run", why=why)
                pause_id = db.record_pause(self.plan_id, first.reason, first.detail or "", [
                    {"contract_id": o.cid, "reason": o.reason, "detail": o.detail or "", "contract_hash": db.contract_hash(o.cid),
                     "evidence": o.evidence} for o in pauses], conflict=any(o.conflict for o in pauses))
                db.clear_stop(self.plan_id)
            self.say(f"{self.plan_id} -> paused ({why})")
            self._investigate_pauses(ctx, pause_id, sorted({o.cid for o in pauses}))
            return self._summary("paused", statuses, [why])
        if all(s == "integrated" for s in statuses.values()):
            rf = self._result_path("final", "integrate")   # its own session, so Ctrl-C cannot fail its setup
            r = self.child([*ateam_argv(), "integrate", self.plan_id, "--result-file", str(rf), "--target", str(ctx.paths.root)], rf)
            reasons = list(r.get("reasons") or ([r["error"]] if r.get("error") else []))
            outcome = {"merge_proposed": "merge_proposed", "refused": "rerun"}.get(r.get("outcome"), "paused")
            if outcome == "paused" and r.get("outcome") == "failed":
                outcome = "rerun"   # integrate failed without recording a pause; re-run it
            for m in reasons:
                self.say(m)
            proposal = Path(r["proposal"]) if r.get("proposal") else None
            return self._summary(outcome, {cid: db.contract_status(cid) for cid in self.cids}, reasons, proposal)
        blockers = [f"{cid} ({s})" for cid, s in statuses.items() if s != "integrated"]
        message = "nothing more can run; unfinished: " + ", ".join(blockers)
        self.say(f"{self.plan_id}: stalled; {message}")
        return self._summary("stalled", statuses, [message])

    def _investigate_pauses(self, ctx, pause_id: int, cids: list[str]) -> None:
        """One read-only INVESTIGATE session per paused contract, after the plan is paused, at `careful` and within the
        run budget; its findings (or a note saying why there are none) wait on the pause record for the owner."""
        db = ctx.db
        usd = self.tiers["careful"][Role.investigate].usd
        for cid in cids:
            def note(text: str) -> None:
                db.set_pause_findings(pause_id, cid, None, None, text)

            worktree = worktree_path(ctx.paths, db, cid)
            if self.stop.is_set():
                note("interrupted"); continue
            if not worktree.exists():
                note("no worktree to investigate"); continue
            verdict = self.budget.reserve(usd, self.stop)   # self.halt is already set by the pause
            if verdict == "exhausted":
                note(f"run budget ${self.budget.limit:.2f} would be exceeded by the investigation"); continue
            if verdict == "halted":
                note("interrupted"); continue
            actual = usd
            try:
                with self.gate.shared():
                    with self._counter_lock:
                        self.sessions += 1
                    r = self.investigate(ctx.paths.root, self.plan_id, cid, worktree, self._result_path(cid, "investigate"))
                cost = r.get("cost_usd")
                actual = usd if cost is None else float(cost)
            finally:
                self.budget.settle(usd, actual)
            outcome = r.get("outcome") or "failed"
            self.say(f"{cid} investigate {outcome} ${actual:.2f}")
            recorded = next((c for c in (db.open_pause(self.plan_id) or {}).get("contracts", []) if c["contract_id"] == cid), None)
            if recorded is not None and recorded["findings"] is None and recorded["note"] is None:
                note(f"investigation {outcome}: {r.get('error') or 'no findings written'}")

    def _startable(self, db, cs, cid: str) -> bool:
        """Ready or failed contracts start only when every in-plan dependency, and the contract before them in their
        lane, is integrated, so nothing is cut before the work it builds on is merged; a verified contract (stopped
        before its merge) is merged at once."""
        status = db.contract_status(cid)
        if status in ("verified", "building"):   # building: only a rebase BUILD an interruption stopped
            return True
        before = [d for d in cs[cid].depends_on if d in self.cids] + ([self.prev[cid]] if self.prev.get(cid) else [])
        return status in ("ready", "failed") and all(db.contract_status(d) == "integrated" for d in before)

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
        try:
            return self._contract(ctx, cid)
        finally:
            ctx.close()   # in this pipeline's own thread: sqlite connections are thread-bound

    def _contract(self, ctx, cid: str) -> _Outcome:
        db = ctx.db
        rebase: list[str] | None = None   # the conflicting paths while this pipeline runs a rebase BUILD
        retest = False                    # a concluded rebase whose TEST or verify an interruption stopped
        prompt: str | None = None
        if db.contract_status(cid) == "verified":
            o = self._merge(ctx, cid)
            if o.kind != "rebase":
                return o
            rebase = o.evidence["paths"]
        elif db.contract_status(cid) == "building":   # a rebase that an interruption, a pause or a crash stopped
            with self.gate.exclusive(), self._refs:
                o = self._continue_rebase(ctx, cid)
            if isinstance(o, _Outcome):
                return o
            rebase = o
            if rebase is None and json.loads(db.get_meta(f"rebase:{cid}") or "{}").get("window") == db.window_start(cid):
                rebase, retest = [], True   # concluded in this window: TEST and verify it again, no BUILD (owner, 2026-10-27)
            elif rebase is None:   # concluded, and the owner resumed since: what failed after it gets an ordinary BUILD
                prompt = _retry_prompt(load_contracts(ctx.paths.contracts)[cid], "the attempt after its rebase was stopped or did not pass")
        c = load_contracts(ctx.paths.contracts)[cid]
        with self._refs:   # not the Gate: nothing a running session's or a verify's tamper check covers (T4, owner 2026-10-28)
            try:
                w = contract_worktree(ctx.paths, db, ctx.git, self.plan_id, cid, after=self.prev.get(cid))
            except WorktreeError as e:
                return _Outcome(cid, "pause", "environment_failure", f"{cid}: {e}", evidence={"error": str(e)})
            if self.prev.get(cid) and not db.attempts_for(cid, role="build"):   # a follower's first BUILD (owner, 2026-10-27)
                left = [path for _, path in GitRepo(w.path).status_porcelain()]
                if left:
                    return _Outcome(cid, "pause", "environment_failure",
                                    f"{cid}: the lane worktree has files left by an earlier contract ({', '.join(left)})",
                                    evidence={"paths": left})
        frozen = c.allocation.tier["build"]
        builds = self._counted_builds(db, cid)
        tier = builds[-1]["tier"] if builds and builds[-1]["tier"] in self.tiers else frozen
        escalated = any(a["tier"] != frozen for a in builds)
        allowed = c.budget.attempts
        if rebase is not None and not retest:
            prompt = _rebase_prompt(c, rebase)
        launched = 0   # BUILDs this pipeline launched that count: one that fails before recording an attempt still does
        while True:
            used = max(len(self._counted_builds(db, cid)), launched)
            if self.halt.is_set():
                return _Outcome(cid, "halted")
            if not retest:
                if used >= allowed:
                    return _Outcome(cid, "pause", "budget_exhausted", f"{cid} has used all {allowed} BUILD attempts")
                r = self._session(ctx, cid, "build", w, tier if tier != frozen else None, prompt, self.tiers[tier][Role.build].usd)
                if isinstance(r, _Outcome):
                    return r
                if r.get("outcome") == "interrupted" or self.stop.is_set():
                    return _Outcome(cid, "interrupted")
                used += 1
                launched = used
                if rebase is not None and not self._merge_concluded(db, cid, w):
                    return _Outcome(cid, "pause", "environment_failure", f"{cid} rebase BUILD did not conclude the merge (build "
                                    + (r.get("outcome") or "failed") + (f": {r['error']}" if r.get("error") else "") + ")",
                                    evidence=r, conflict=True)
                o = StepOutcome("build", int(r.get("code", 1)), r.get("outcome") or "failed", subtype=r.get("subtype"),
                                refusal=r.get("refusal"))
                d = decide(classify(o), used, allowed, tier, escalated, o.subtype)
                if rebase is not None and d.action != "continue":   # one attempt: no retry after a rebase BUILD
                    return _Outcome(cid, "pause", "environment_failure", f"{cid} rebase build {o.outcome}", evidence=r)
                if d.action == "pause":
                    return _Outcome(cid, "pause", d.reason, f"{cid} build {o.outcome}" + (f" ({r['error']})" if r.get("error") else ""),
                                    evidence=r)
                if d.action == "retry":
                    escalated, tier = escalated or d.tier != tier, d.tier
                    prompt = _retry_prompt(c, f"the BUILD attempt ended {o.outcome}" + (f": {r['error']}" if r.get("error") else ""))
                    continue

            retest = False
            # TEST never runs below BUILD (spec, Allocation), including after an escalation
            test_tier = at_least(tier, c.allocation.tier["test"])
            retry_build = False
            for stage_used in range(allowed + 1):
                if self.halt.is_set():
                    return _Outcome(cid, "halted")
                r = self._session(ctx, cid, "test", w, test_tier if test_tier != c.allocation.tier["test"] else None, None,
                                  self.tiers[test_tier][Role.test].usd)
                if isinstance(r, _Outcome):
                    return r
                if r.get("outcome") == "interrupted" or self.stop.is_set():
                    return _Outcome(cid, "interrupted")
                o = StepOutcome("test", int(r.get("code", 1)), r.get("outcome") or "failed", subtype=r.get("subtype"),
                                test_class=r.get("test_class"), refusal=r.get("refusal"))
                d = decide(classify(o), used, allowed, tier, escalated, None, stage="test", stage_used=stage_used + 1)
                if rebase is not None and d.action != "continue":
                    return _Outcome(cid, "pause", "environment_failure",
                                    f"{cid} test {r.get('test_outcome') or o.outcome} after its rebase BUILD", evidence=r)
                if d.action == "retry_stage":
                    continue
                if d.action == "pause":
                    return _Outcome(cid, "pause", d.reason, f"{cid} test {r.get('test_outcome') or o.outcome}", evidence=r)
                if d.action == "retry":
                    prompt = _retry_prompt(c, "the TEST worker judged these criteria not met:\n" + _unmet(r.get("report")))
                    retry_build = True
                break
            if retry_build:
                continue

            for stage_used in range(allowed + 1):
                if self.halt.is_set():
                    return _Outcome(cid, "halted")
                with self._writing(ctx, refs=False):   # beside sessions, never beside another writer (owner, 2026-10-28)
                    rf = self._result_path(cid, "verify")
                    v = self.child([*ateam_argv(), "verify", cid, "--cwd", str(w.path), "--base", w.base, "--result-file", str(rf),
                                    "--target", str(ctx.paths.root)], rf)
                self.say(f"{cid} verify {v.get('status')}" + (f" ({v['failure_class']})" if v.get("failure_class") else ""))
                o = StepOutcome("verify", int(v.get("code", 1)), v.get("status") or "failed", verify_status=v.get("status"),
                                failure_class=v.get("failure_class"), refusal=v.get("refusal"))
                d = decide(classify(o), used, allowed, tier, escalated, None, stage="verify", stage_used=stage_used + 1)
                if rebase is not None and d.action != "continue":
                    return _Outcome(cid, "pause", "environment_failure", f"{cid} verify {v.get('status')} after its rebase BUILD: "
                                    + "; ".join(v.get("reasons") or []), evidence=v)
                if d.action == "retry_stage":
                    continue
                if d.action == "pause":
                    return _Outcome(cid, "pause", d.reason, f"{cid} verify {v.get('status')}: " + "; ".join(v.get("reasons") or []),
                                    evidence=v)
                if d.action == "retry":
                    prompt = _retry_prompt(c, "verify failed: " + "; ".join(v.get("reasons") or ["no reason given"]))
                    retry_build = True
                break
            if retry_build:
                continue
            if db.contract_status(cid) != "verified":   # the store, not the child's report, decides what is merged
                return _Outcome(cid, "pause", "environment_failure",
                                f"{cid} verify reported verified but the state store says {db.contract_status(cid)}", evidence=v)
            o = self._merge(ctx, cid)
            if o.kind != "rebase":
                return o
            rebase, prompt = o.evidence["paths"], _rebase_prompt(c, o.evidence["paths"])
            with self.gate.exclusive(), self._refs:
                w = contract_worktree(ctx.paths, db, ctx.git, self.plan_id, cid)   # its base is now the integration tip

    @contextmanager
    def _writing(self, ctx, refs: bool = True) -> Iterator[None]:
        """A writer step (merge, refresh, verify) beside running sessions (overlap gate design, owner 2026-10-28): it
        reads the intent HEAD inside its locks, marks itself active, and records its own `ateam:` intent commits in a
        finally, so a session's intent check accepts exactly those. Callers take it; _refresh never does."""
        with self.gate.writer(), (self._refs if refs else _no_lock()):
            intent = intent_git(ctx.paths)
            before = intent.head()
            ctx.db.set_meta(WRITER_KEY, self.plan_id)
            try:
                yield
            finally:
                try:
                    record_coordinator_commits(ctx.db, intent, before)
                finally:
                    ctx.db.delete_meta(WRITER_KEY)

    def _blocks(self, db, cid: str) -> str:
        """`; blocks C-x, C-y (same lane)` for the unfinished contracts after `cid` in its lane, which build on it."""
        lane = next((lane for lane in self.lanes if cid in lane), [cid])
        later = [c for c in lane[lane.index(cid) + 1:] if db.contract_status(c) != "integrated"]
        return f"; blocks {', '.join(later)} (same lane)" if later else ""

    def _counted_builds(self, db, cid: str) -> list[dict]:
        """BUILD attempts that count toward budget.attempts: those since the contract's latest resume window, and never
        an interrupted one (owner, 2026-10-26)."""
        start = db.window_start(cid)
        return [a for a in db.attempts_for(cid, role="build") if a["attempt_id"] > start and a.get("outcome") != "interrupted"]

    def _session(self, ctx, cid: str, role: str, w, tier: str | None, prompt: str | None, usd: float) -> dict | _Outcome:
        verdict = self.budget.reserve(usd, self.halt)
        if verdict == "halted":
            return _Outcome(cid, "halted")
        if verdict == "exhausted":
            self.halt.set()
            return _Outcome(cid, "budget", None, f"run budget ${self.budget.limit:.2f} would be exceeded by {cid} {role} "
                                                 f"(spent ${self.budget.spent:.2f}, cap ${usd:.2f})")
        actual = usd
        try:
            with self.gate.shared():
                # stop or a pause arrived while this session waited. Ctrl-C sets `stop` at once; `halt` follows only on
                # the main loop's next poll, and a verify no longer holds sessions back until then (owner, 2026-10-28)
                if self.halt.is_set() or self.stop.is_set():
                    actual = 0.0
                    return _Outcome(cid, "halted")
                with self._counter_lock:
                    self.sessions += 1
                r = self.launch(ctx.paths.root, cid, role, w.path, w.base, tier, prompt, self._result_path(cid, role))
            cost = r.get("cost_usd")
            actual = usd if cost is None else float(cost)   # unknown cost (a crash) counts the whole cap
        finally:
            self.budget.settle(usd, actual)
        self.say(f"{cid} {role} {r.get('outcome')} ${actual:.2f}" + (f" tier {tier}" if tier else "")
                 + (f" (log {r['log']})" if r.get("code") not in (0, None) and r.get("log") else ""))
        return r

    def _merge(self, ctx, cid: str) -> _Outcome:
        with self._writing(ctx):
            wt = GitRepo(integration_worktree(ctx.paths, self.plan_id))
            try:
                merge_contract(ctx, wt, self.plan_id, cid, self.say)
            except MergeConflict as e:
                return self._conflict(ctx, cid, list(e.paths))   # inside the writer step: neither lock is reentrant
            self._refresh(ctx)
        return _Outcome(cid, "done")

    def _conflict(self, ctx, cid: str, paths: list[str]) -> _Outcome:
        """A verified contract's merge conflicted: start its one rebase BUILD (kind `rebase`) or pause as a conflict
        (owner, 2026-10-27). Runs inside _merge's exclusive step."""
        db = ctx.db
        c = load_contracts(ctx.paths.contracts)[cid]
        uneditable = [p for p in paths if not any(matches(g, p) for g in c.paths)]
        rebased = rebased_this_window(db, cid)
        d = decide_conflict(rebased, len(self._counted_builds(db, cid)), c.budget.attempts, uneditable)
        if d.action == "pause":
            why = (f"{', '.join(uneditable)} are outside the contract's paths" if uneditable
                   else "again after its rebase BUILD" if rebased else "no BUILD attempt left")
            return _Outcome(cid, "pause", d.reason, f"{cid} conflicts in {', '.join(paths)}; {why}",
                            evidence={"paths": paths, **({"uneditable": uneditable} if uneditable else {})}, conflict=True)
        if self.halt.is_set():
            return _Outcome(cid, "halted")
        try:
            _, conflicts = prepare_rebase(ctx.paths, db, ctx.git, self.plan_id, cid, ctx.git.rev_parse(integration_branch(self.plan_id)))
            tip = json.loads(db.get_meta(f"rebase:{cid}"))["tip"]
            reopen_for_rebase(ctx, self.plan_id, cid, tip, self.say)
        except (GitError, WorktreeError, IntentError) as e:
            return _Outcome(cid, "pause", "environment_failure", f"{cid} rebase could not start: {e}", evidence={"paths": paths},
                            conflict=True)
        self.say(f"{cid} conflicts in {', '.join(paths)}; one rebase BUILD onto {tip[:12]}")
        return _Outcome(cid, "rebase", evidence={"paths": conflicts or paths})

    def _continue_rebase(self, ctx, cid: str) -> list[str] | None | _Outcome:
        """A contract left building by a rebase: the conflicting paths when its merge is still open (the rebase BUILD
        runs again), None when the merge was concluded, or a pause. Runs inside an exclusive step."""
        db = ctx.db
        wt = GitRepo(worktree_path(ctx.paths, db, cid))
        tip = json.loads(db.get_meta(f"rebase:{cid}") or "{}").get("tip")
        if tip and not wt.merge_in_progress() and wt.is_ancestor(tip, "HEAD"):
            return None   # its merge was concluded; TEST, verify or an interruption stopped it after
        try:
            _, conflicts = prepare_rebase(ctx.paths, db, ctx.git, self.plan_id, cid, ctx.git.rev_parse(integration_branch(self.plan_id)))
        except (GitError, WorktreeError) as e:
            return _Outcome(cid, "pause", "environment_failure", f"{cid} rebase could not continue: {e}", conflict=True)
        return conflicts if wt.merge_in_progress() else None

    def _merge_concluded(self, db, cid: str, w) -> bool:
        """The rebase BUILD concluded the merge the scheduler started: none in progress, and HEAD holds the recorded tip."""
        g = GitRepo(w.path)
        return not g.merge_in_progress() and g.is_ancestor(json.loads(db.get_meta(f"rebase:{cid}"))["tip"], "HEAD")

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


def _rebase_prompt(contract, paths: list[str]) -> str:
    return (f"Resolve a merge conflict for contract {contract.id}: {contract.title}. The scheduler merged the plan's integration "
            f"branch into this worktree and left conflict markers in: {', '.join(paths) or '(none left; the merge is still open)'}. "
            "Resolve them keeping the behaviour of both sides and this contract's intent, change nothing else, and conclude "
            "the merge with git commit. Follow the brief in your system prompt and end with the structured report.")


def _retry_prompt(contract, why: str) -> str:
    return (f"Implement contract {contract.id}: {contract.title}. Follow the brief in your system prompt and end with the "
            f"structured report. The previous attempt did not pass: {why}")
