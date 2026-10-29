"""ateam run: execute an approved plan until the owner's merge gate or a pause (spec "Architecture: scheduler").

The scheduler drives the commands that exist: `ateam worker run` for BUILD and TEST sessions (subprocesses in this
process group, so a terminal Ctrl-C reaches them), `ateam verify` and `ateam contracts refresh` (subprocesses in their
own session, so they finish), and integrate's helpers to merge each verified contract onto ateam/<plan-id> at once.
Every child reports through a --result-file. Sessions run in parallel up to config.max_workers; every step that
commits to the intent repo or creates refs runs alone under a writer-preferring gate, so the tamper checks of
sessions and verifies never see another step's commit (owner, 2026-10-26). Retries follow ateam.retry; a run spends
at most config.run_budget_usd on sessions. `ateam run` never merges into the target branch.

Contracts whose declared files overlap form a lane (design 2026-10-27); a dependency alone never joins lanes, so the
dependents of one contract fan out once it is integrated (owner decision 4, 2026-10-28). A lane runs its contracts one
at a time in one shared worktree, each based on the integration tip, and lanes run in parallel up to
config.max_workers. A contract starts only when its in-plan dependencies are integrated. A lane member that has started
and is not integrated holds its lane, decided from the store so a re-run keeps it; otherwise the lane's next contract
is the first ready one, so a contract waiting on another lane does not block the rest of its own. A verified contract
whose merge conflicts gets one rebase BUILD in its own worktree, then TEST and verify again (owner, 2026-10-27).
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

from ateam.checkpoint import collect
from ateam.contracts.select import drift
from ateam.contracts.store import load_contracts
from ateam.events import prune_logs
from ateam.gitops import GitError, GitRepo, MergeConflict
from ateam.globs import matches
from ateam.treecheck import DEFAULT_IGNORE
from ateam.guidance import GuidanceEntry, live_guidance, load_guidance
from ateam.integrate import (IntegrateRefused, integration_branch, integration_worktree, merge_contract, prepare_integration,
                             reconcile_contracts, reconcile_feature, reopen_for_rebase)
from ateam.intent import WRITER_KEY, IntentError, intent_git, record_coordinator_commits, require_clean
from ateam.paths import home_dir
from ateam.plans.ops import dependency_order, plan_contract_ids, set_plan_status
from ateam.plans.store import load_plans
from ateam.records.store import load_records
from ateam.retry import StepOutcome, classify, decide, decide_conflict, next_tier
from ateam.roles import Role
from ateam.state.db import StateDB
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
    capped_sessions: int = 0   # sessions this run settled at their tier cap because the cost was unknown


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


def _prune_logs(ctx, say: Callable[[str], None]) -> None:
    """Remove event logs of attempts finished more than config.log_retention_days ago. Never commits to the intent repo,
    so it is not a writer step; a failure is reported in one line and the run goes on."""
    days = ctx.config.log_retention_days
    try:
        removed = prune_logs(ctx.paths.logs, ctx.db.telemetry_rows(), days, datetime.now(UTC))
    except Exception as e:  # noqa: BLE001  (a locked state.db as much as a file error; the log is never needed to run)
        say(f"could not prune event logs: {e}")
        return
    if removed:
        say(f"pruned {len(removed)} event log(s) older than {days} day(s)")


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
    matches a glob of each, or when a glob of one names the other's glob taken as a path (a file not created yet); a
    lane is the connected group. A dependency alone joins nothing (owner decision 4, 2026-10-28). Each lane is in
    dependency order, ties by id, and lanes are ordered by their first contract."""
    globs = {cid: [*contracts[cid].paths, *contracts[cid].incidental] for cid in cids}
    hits = {cid: {f for f in files if any(matches(g, f) for g in globs[cid])} for cid in cids}
    parent = {cid: cid for cid in cids}

    def root(cid: str) -> str:
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid

    def overlap(a: str, b: str) -> bool:
        if hits[a] & hits[b]:
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
        self.sessions, self.capped = 0, 0
        self._counter, self._counter_lock = 0, threading.Lock()
        self._refs = threading.Lock()   # ATeam's own git ref and worktree creation, one at a time; taken after the Gate
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.runs_dir = fresh_dir(home_dir(ctx.paths.root) / "runs" / plan_id, f"{stamp}-{os.getpid()}")   # never reused
        self.lanes: list[list[str]] = []
        self.running: dict[str, threading.Thread] = {}

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
        _prune_logs(ctx, self.say)   # only once integration setup passed: a refused run prunes nothing (owner, 2026-10-29)

        running = self.running
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
                for cid in order:   # one at a time: _startable sees each launch, so a lane never gets two
                    if len(running) >= ctx.config.max_workers:
                        break
                    if cid in running or cid in settled or not self._startable(db, cs, cid):
                        continue
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
                if cost is None:
                    with self._counter_lock:
                        self.capped += 1
            finally:
                self.budget.settle(usd, actual)
            outcome = r.get("outcome") or "failed"
            self.say(f"{cid} investigate {outcome} ${actual:.2f}")
            recorded = next((c for c in (db.open_pause(self.plan_id) or {}).get("contracts", []) if c["contract_id"] == cid), None)
            if recorded is not None and recorded["findings"] is None and recorded["note"] is None:
                note(f"investigation {outcome}: {r.get('error') or 'no findings written'}")

    def _lane(self, cid: str) -> list[str]:
        return next((lane for lane in self.lanes if cid in lane), [cid])

    def _started(self, db, cid: str) -> bool:
        """Durable, from the store: the contract was cut, has a BUILD attempt, or its status is past ready."""
        return (db.get_meta(f"base:{cid}") is not None or bool(db.attempts_for(cid, role="build"))
                or db.contract_status(cid) in ("building", "verifying", "verified", "failed", "paused"))

    def _startable(self, db, cs, cid: str) -> bool:
        """A contract runs when it is its lane's next (owner decision 4, 2026-10-28): the lane member that has started
        and is not integrated holds the lane, across re-runs; with none, the first in lane order that is ready or failed
        with every in-plan dependency integrated. A verified contract (stopped before its merge) is merged at once and
        a building one continues its rebase. Two members of a lane never run at once."""
        lane = self._lane(cid)
        if any(m in self.running for m in lane if m != cid):   # one just launched may not have recorded its start yet
            return False
        holder = next((m for m in lane if db.contract_status(m) != "integrated" and self._started(db, m)), None)
        if holder is not None and holder != cid:
            return False
        status = db.contract_status(cid)
        if holder == cid and status in ("verified", "building"):   # building: only a rebase BUILD an interruption stopped
            return True
        due = lambda m: (db.contract_status(m) in ("ready", "failed")   # noqa: E731
                         and all(db.contract_status(d) == "integrated" for d in cs[m].depends_on if d in self.cids))
        return due(cid) and (holder == cid or next((m for m in lane if due(m)), None) == cid)

    def _after(self, db, cid: str) -> str | None:
        """The lane member already in the lane worktree (its lane head recorded), or None when none has been cut."""
        return next((m for m in self._lane(cid) if m != cid and db.get_meta(f"lane:{m}") is not None), None)

    def _summary(self, outcome, statuses, messages, proposal=None) -> RunSummary:
        self.say(f"summary: {outcome}; {self.sessions} session(s), {self.capped} settled at the tier cap (cost unknown), "
                 f"${self.budget.spent:.2f} spent; " + ", ".join(f"{c} {s}" for c, s in statuses.items()))
        return RunSummary(self.plan_id, outcome, statuses, self.budget.spent, self.sessions, messages, proposal, self.capped)

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
        start = "build"                   # build, test (a concluded rebase) or verify (TEST passed on this commit)
        reentry = False                   # a rebase merge left open by a stopped pipeline: concluded here if resolved
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
            rebase, reentry = o, o is not None
            if rebase is None and json.loads(db.get_meta(f"rebase:{cid}") or "{}").get("window") == db.window_start(cid):
                rebase, start = [], "test"   # concluded in this window: TEST and verify it again, no BUILD (owner, 2026-10-27)
            elif rebase is None:   # concluded, and the owner resumed since: what failed after it gets an ordinary BUILD
                prompt = _retry_prompt(load_contracts(ctx.paths.contracts)[cid], "the attempt after its rebase was stopped or did not pass")
        c = load_contracts(ctx.paths.contracts)[cid]
        with self._refs:   # not the Gate: nothing a running session's or a verify's tamper check covers (T4, owner 2026-10-28)
            try:
                after = self._after(db, cid)
                w = contract_worktree(ctx.paths, db, ctx.git, self.plan_id, cid, after=after)
            except WorktreeError as e:
                return _Outcome(cid, "pause", "environment_failure", f"{cid}: {e}", evidence={"error": str(e)})
            if after is not None and not db.attempts_for(cid, role="build"):   # a follower's first BUILD (owner, 2026-10-27)
                left = [path for _, path in GitRepo(w.path).status_porcelain()]
                if left:
                    return _Outcome(cid, "pause", "environment_failure",
                                    f"{cid}: the lane worktree has files left by an earlier contract ({', '.join(left)})",
                                    evidence={"paths": left})
        if reentry:   # a BUILD resolved and staged the merge, then a stop or a crash came before the conclusion
            why = self._conclude_rebase(ctx, cid, w)
            if why is None:
                start = "test"   # concluded: TEST and verify it, no BUILD
            else:
                self.say(f"{cid} rebase merge not concluded ({why}); the rebase BUILD runs again")
        frozen = c.allocation.tier["build"]
        builds = self._counted_builds(db, cid)
        tier = builds[-1]["tier"] if builds and builds[-1]["tier"] in self.tiers else frozen
        escalated = any(a["tier"] != frozen for a in builds)
        allowed = c.budget.attempts
        if rebase is None or start == "test":   # not about to run a rebase BUILD: start after the last step that passed
            step, why = restart_point(db, cid, w.path, db.contract_hash(cid), load_guidance(ctx.paths.guidance, cid))
            if step == "verify":
                start, prompt = "verify", None
            if step == "verify" or (rebase is None and db.step_results(cid)):
                self.say(f"{cid} restarts at {step}: {why}")
        if rebase is not None and start == "build":
            prompt = _rebase_prompt(c, rebase)
        launched = 0   # BUILDs this pipeline launched that count: one that fails before recording an attempt still does
        while True:
            first, start = start, "build"
            used = max(len(self._counted_builds(db, cid)), launched)
            if self.halt.is_set():
                return _Outcome(cid, "halted")
            if first == "build":
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
                    why = self._conclude_rebase(ctx, cid, w) if r.get("outcome") in ("completed", "scope_breach") else None
                    if why is not None or not self._merge_concluded(db, cid, w):
                        return _Outcome(cid, "pause", "environment_failure", f"{cid} rebase BUILD did not conclude the merge (build "
                                        + (r.get("outcome") or "failed") + (f": {r['error']}" if r.get("error") else "") + ")"
                                        + (f": {why}" if why else ""), evidence=r, conflict=True)
                    r = {**r, "code": 0, "outcome": "completed"}   # as its attempt row now says
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

            # TEST never runs below BUILD (spec, Allocation), including after an escalation
            test_tier = at_least(tier, c.allocation.tier["test"])
            retry_build = False
            for stage_used in range(allowed + 1 if first != "verify" else 0):
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
                if d.action == "continue" and r.get("test_outcome") == "completed" and o.code == 0:
                    self._record(db, cid, "test", w, True)   # only on a clean tree
                elif d.action in ("retry", "pause"):
                    self._record(db, cid, "test", w, False)
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
                if db.contract_status(cid) == "verified":
                    self._record(db, cid, "verify", w, True)
                elif d.action in ("retry", "pause"):
                    self._record(db, cid, "verify", w, False)
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
        """`; blocks C-x, C-y (same lane)` for the lane's other unintegrated members: `cid` holds the lane until it is
        integrated."""
        others = [c for c in self._lane(cid) if c != cid and db.contract_status(c) != "integrated"]
        return f"; blocks {', '.join(others)} (same lane)" if others else ""

    def _record(self, db, cid: str, step: str, w, passed: bool) -> None:
        """A TEST or verify result at the worktree's HEAD as it is now, after the step (C-0094). A pass on a tree with
        uncommitted changes is not recorded: it says nothing about any commit."""
        g = GitRepo(w.path)
        head = g.head()
        if passed and step == "test" and _dirty(g):
            return
        tests = db.attempts_for(cid, role="test") if step == "test" else []
        db.record_step(cid, step, head, db.contract_hash(cid) or "", passed, tests[-1]["attempt_id"] if tests else None)

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
            if cost is None:
                with self._counter_lock:
                    self.capped += 1
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

    def _conclude_rebase(self, ctx, cid: str, w) -> str | None:
        """Conclude the rebase merge a BUILD resolved and staged but left open: None when concluded, else the reason
        naming the failed condition (rebase_conclusion_problem, or the git error of a commit a hook rejected, which
        leaves the merge open). Only after the contract's latest BUILD attempt, newer than the rebase, ended completed
        or scope_breach (the merge-staged files of other contracts breach scope until the commit); that attempt becomes
        completed and gets a fresh checkpoint against the tip. A scheduler step under the exclusive lock the rebase
        sites use: it commits only the contract's worktree, never the intent repo."""
        with self.gate.exclusive(), self._refs:
            db = ctx.db
            meta = json.loads(db.get_meta(f"rebase:{cid}") or "{}")
            tip, g = meta.get("tip"), GitRepo(w.path)
            if not tip or g.merge_head() != tip:
                return "no merge onto the recorded tip is open"
            builds = db.attempts_for(cid, role="build")
            build = builds[-1] if builds else None
            if build is None or build["attempt_id"] <= meta["after"] or build.get("outcome") not in ("completed", "scope_breach"):
                return "no completed BUILD attempt since the rebase began"
            # Only within that BUILD's own window: after a resume, or guidance as new as it, a BUILD runs again so the
            # owner's note reaches one (owner, 2026-10-29; P-0020 review).
            if build["attempt_id"] <= db.window_start(cid):
                return "the owner resumed since the rebase BUILD"
            done = datetime.fromisoformat(build["finished_at"])
            if any((e.at if e.at.tzinfo else e.at.replace(tzinfo=UTC)) >= done
                   for e in live_guidance(load_guidance(ctx.paths.guidance, cid), db.contract_hash(cid) or "")):
                return "new guidance since the rebase BUILD"
            c = load_contracts(ctx.paths.contracts)[cid]
            conflicted = meta["paths"] if "paths" in meta else g.unmerged_paths()   # meta written before 'paths' was
            problem = rebase_conclusion_problem(g, tip, conflicted, [*c.paths, *c.incidental])
            if problem is not None:
                return problem
            try:
                head = g.commit_no_edit()
            except GitError as e:
                return f"git commit failed: {e}"
            if build["outcome"] != "completed":
                db.set_attempt_outcome(build["attempt_id"], "completed", None)
            scratch = ctx.paths.scratch_root / cid   # the BUILD's scratch directory (runner.run.prepare_scratch)
            cp = collect(g, scratch, tip, c, load_records(ctx.paths.records), build.get("session_id"), checks=[])
            db.set_checkpoint(cid, build["attempt_id"], cp.to_dict())
            self.say(f"{cid} rebase merge concluded at {head[:12]}")
            return None

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


def restart_point(db: StateDB, cid: str, worktree: Path | None, contract_hash: str | None,
                  guidance: list[GuidanceEntry]) -> tuple[str, str]:
    """Where a re-run or a resume starts `cid`, and why (C-0094): done when integrated, merge when verified, rebase
    while a rebase merge is open, verify when TEST passed on the worktree's HEAD and nothing since says otherwise,
    otherwise build. A concluded rebase with no such pass keeps its rules: TEST again within the window it was
    started in, an ordinary BUILD after the owner resumed."""
    status = db.contract_status(cid)
    if status == "integrated":
        return "done", "integrated"
    if status == "verified":
        return "merge", "verified"
    exists = worktree is not None and worktree.exists()
    meta = json.loads(db.get_meta(f"rebase:{cid}") or "{}") if status == "building" else None
    if meta is not None:
        tip = meta.get("tip")
        g = GitRepo(worktree) if exists else None
        if not tip or g is None or g.merge_in_progress() or not g.is_ancestor(tip, "HEAD"):
            return "rebase", f"the rebase merge onto {tip[:8] if tip else 'the integration tip'} is still open"
    if not exists:
        return "build", "no worktree yet"
    step, why = _after_pass(db, cid, GitRepo(worktree), contract_hash, guidance)
    if meta is None or step == "verify":
        return step, why
    if meta.get("window") == db.window_start(cid):
        return "test", "rebase concluded; TEST and verify it again"
    return "build", "rebase concluded and the owner resumed since; an ordinary BUILD"


def _after_pass(db: StateDB, cid: str, g: GitRepo, contract_hash: str | None,
                guidance: list[GuidanceEntry]) -> tuple[str, str]:
    """verify when the latest TEST pass is on HEAD, the tree is clean, the frozen hash is the one it passed on, no
    TEST or verify failed on HEAD since, and no live guidance is as new as the pass; build, naming the first that
    fails, otherwise."""
    rows = db.step_results(cid)
    passes = [r for r in rows if r["step"] == "test" and r["passed"]]
    if not passes:
        return "build", "no TEST pass recorded"
    last, head = passes[-1], g.head()
    if last["head"] != head:
        return "build", f"HEAD {head[:8]} is not {last['head'][:8]}, where TEST passed"
    if _dirty(g):
        return "build", "the worktree has uncommitted changes"
    if contract_hash is None or last["contract_hash"] != contract_hash:
        return "build", "re-frozen since TEST passed"
    failed = [r for r in rows if r["seq"] > last["seq"] and r["head"] == head and not r["passed"]]
    if failed:
        return "build", f"{failed[-1]['step']} failed on {head[:8]} after TEST passed"
    at = datetime.fromisoformat(last["at"])
    newer = [e for e in live_guidance(guidance, contract_hash) if (e.at if e.at.tzinfo else e.at.replace(tzinfo=UTC)) >= at]
    if newer:
        return "build", f"new guidance from {newer[-1].source} since TEST passed"
    return "verify", f"TEST passed on {head[:8]}"


def _unmet(report: dict | None) -> str:
    verdicts = (report or {}).get("verdicts") or []
    lines = [f"- {v.get('id')}: {v.get('evidence') or v.get('verdict')}" for v in verdicts if v.get("verdict") != "met"]
    return "\n".join(lines) or "- (no evidence reported)"


def _rebase_prompt(contract, paths: list[str]) -> str:
    return (f"Resolve a merge conflict for contract {contract.id}: {contract.title}. The scheduler merged the plan's integration "
            f"branch into this worktree and left conflict markers in: {', '.join(paths) or '(none left; the merge is still open)'}. "
            "Resolve them keeping the behaviour of both sides and this contract's intent, and change nothing else. You must "
            "then conclude the merge yourself: stage the resolved files and run `git commit` (git commit --no-edit keeps the "
            "merge message). If the merge is still open when you finish, the plan pauses. Follow the brief in your system "
            "prompt and end with the structured report.")


def _marker_line(line: str) -> bool:
    return line == "=======" or (line[:7] in ("<<<<<<<", "|||||||", ">>>>>>>") and line[7:8] in ("", " "))


def _ignored(path: str) -> bool:
    return any(matches(x, path) for x in DEFAULT_IGNORE)


def _dirty(g: GitRepo) -> list[tuple[str, str]]:
    """Porcelain entries the scope check counts: .claude/** is ignored here as in treecheck.scope_check (owner,
    2026-10-29; P-0020 review)."""
    return [(xy, p) for xy, p in g.status_porcelain() if not _ignored(p)]


def rebase_conclusion_problem(g: GitRepo, tip: str, conflicted: list[str], globs: list[str]) -> str | None:
    """Why an open rebase merge onto `tip` may not be concluded, or None. Measured on git, not on the porcelain scope
    check (which counts the files the tip brought in): (a) no path is unmerged; (b) no tracked file among the merge's
    `conflicted` paths holds a marker line; (c) `git diff --name-only <tip>` lies inside `globs` (the contract's paths
    plus incidental); (d) nothing is unstaged or untracked. Entries staged cleanly from the tip are allowed."""
    unmerged = g.unmerged_paths()
    if unmerged:
        return f"unmerged path {', '.join(unmerged)}"
    tracked = set(g.run("ls-files", "-z", "--", *conflicted).split("\0")) if conflicted else set()
    for rel in conflicted:
        f = g.root / rel
        if rel in tracked and f.is_file() and any(_marker_line(x) for x in f.read_text(errors="replace").splitlines()):
            return f"marker in {rel}"
    outside = [p for p in g.diff_names(tip) if not any(matches(x, p) for x in globs) and not _ignored(p)]
    if outside:
        return f"{', '.join(outside)} outside the contract's paths"
    loose = [p for xy, p in _dirty(g) if xy == "??" or xy[1] != " "]
    if loose:
        return f"unstaged or untracked {', '.join(loose)}"
    return None


def _retry_prompt(contract, why: str) -> str:
    return (f"Implement contract {contract.id}: {contract.title}. Follow the brief in your system prompt and end with the "
            f"structured report. The previous attempt did not pass: {why}")
