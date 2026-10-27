"""The ateam command-line interface."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import click

from ateam import SPIKED_CLI_VERSION, __version__
from ateam.brief import DEFAULT_MAX_CANDIDATES, assemble_investigation_brief
from ateam.config import TargetConfig, load_config
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir, lookup_target, normalise_remote, origin_url, record_target, repo_id, repo_id_source
from ateam.intent import IntentError, commit_intent, commit_intent_files, intent_relpath, require_clean
from ateam.runner.investigate import INTENT_TAMPER, run_investigation
from ateam.exclusion import process_alive as _process_alive  # noqa: F401  (moved; kept importable from here)
from ateam.state.db import StateDB

TARGET_OPTION = click.option(
    "--target", "target", type=click.Path(file_okay=False, path_type=Path), default=Path.cwd,
    show_default="current directory", help="Target repository root.",
)


class Context:
    def __init__(self, target: Path) -> None:
        self.paths = TargetPaths.from_root(target)
        self.git = GitRepo(target)
        self.config: TargetConfig = load_config(self.paths)
        self.db = StateDB(home_dir(target) / "state.db")
        self.db.init_schema()
        command = click.get_current_context(silent=True)   # thread-local: a scheduler thread's Context has none
        if command is not None:
            command.call_on_close(self.close)   # closed when the command ends, including on sys.exit

    def close(self) -> None:
        self.db.close()


def _previous_home(target: Path) -> tuple[dict, Path] | None:
    """(registry entry, old home dir) when this target's id changed and the old home still has an intent repo."""
    previous = lookup_target(target)
    if not previous or previous["id"] == repo_id(target):
        return None
    old_home = home_dir(target).parent / previous["id"]
    return (previous, old_home) if (old_home / "intent" / ".git").exists() else None


def _context(target: Path, require_ateam: bool = True) -> Context:
    if require_ateam and not TargetPaths.from_root(target).exists():
        moved = _previous_home(target)
        if moved:
            previous, old_home = moved
            click.echo(f"The intent repo for {target} exists under a previous id {previous['id']} (derived from {previous['source']}); "
                       f"the id is now {repo_id(target)} (from {_derivation(target)}). Run: ateam intent init --adopt  to move it, "
                       f"or ateam intent init --fresh to start a new one and keep {old_home} as is")
        else:
            click.echo(f"No intent repo for {target}. Run: ateam intent init")
        sys.exit(2)
    return Context(target)   # closed by click when the command ends (Context.__init__)


_LAST_FAIL: list[str] = []   # the message of the last _fail, for --result-file


def _fail(message: str, code: int = 1) -> None:
    _LAST_FAIL[:] = [message]
    click.echo(f"error: {message}")
    sys.exit(code)


RESULT_FILE_OPTION = click.option(
    "--result-file", "result_file", type=click.Path(dir_okay=False, path_type=Path), default=None,
    help="Also write the outcome as JSON to this file (read by `ateam run`).",
)


class _Result:
    """Collects a command's outcome fields and, when a path is given, writes them with the exit code as JSON on any
    exit. A refusal's message (from _fail) fills `reasons` or `error` when the command set neither."""

    def __init__(self, path: Path | None, defaults: dict) -> None:
        self.path, self.data = path, dict(defaults)

    def __enter__(self) -> dict:
        _LAST_FAIL.clear()
        return self.data

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.path is not None:
            import json

            code = (exc.code if exc.code is not None else 0) if isinstance(exc, SystemExit) else (0 if exc is None else 1)
            data = {"code": code, **self.data}
            if isinstance(exc, KeyboardInterrupt):   # Ctrl-C: never to be read as a refusal (owner, 2026-10-26)
                data["code"] = 130
                data.update({k: "interrupted" for k in ("outcome", "status") if k in data})
            elif exc is not None and not isinstance(exc, SystemExit):   # a crash is a failure, not a refusal
                data.update({k: "failed" for k in ("outcome", "status") if k in data})
                if "failure_class" in data:
                    data["failure_class"] = "environment"
                if "error" in data:
                    data["error"] = f"{type(exc).__name__}: {exc}"
                if "cost_usd" in data:
                    data["cost_usd"] = None   # unknown: the scheduler counts the session's whole cap
            if "refusal" in data and data["refusal"] is None and "refused" in (data.get("status"), data.get("outcome")):
                data["refusal"] = "other"
            if _LAST_FAIL and "reasons" in data and not data["reasons"]:
                data["reasons"] = list(_LAST_FAIL)
            if _LAST_FAIL and "error" in data and data["error"] is None:
                data["error"] = _LAST_FAIL[0]
            Path(self.path).write_text(json.dumps(data, default=str))
        return False


class _Group(click.Group):
    """Turns store errors (malformed YAML under .ateam/) into one error line for every command."""

    def invoke(self, ctx: click.Context):  # type: ignore[override]
        from ateam.contracts.store import ContractStoreError
        from ateam.records.store import RecordStoreError

        try:
            return super().invoke(ctx)
        except (RecordStoreError, ContractStoreError) as e:
            click.echo(f"error: {e}")
            sys.exit(1)


@click.group(cls=_Group)
@click.version_option(__version__, prog_name="ateam")
def main() -> None:
    """ATeam: durable intent, role-separated workers, human approval gates."""


# ---------------------------------------------------------------- intent


@main.group()
def intent() -> None:
    """The private intent repo for a target (records, contracts, config)."""


@intent.command("init")
@click.option("--remote", default=None, help="Private git remote to pull from and push to.")
@click.option("--adopt", is_flag=True, help="After the target's id changed (for example a new origin), move the existing home to the new id.")
@click.option("--fresh", is_flag=True, help="After the target's id changed, start a new intent repo and leave the old home alone.")
@TARGET_OPTION
def intent_init(remote: str | None, adopt: bool, fresh: bool, target: Path) -> None:
    """Create the intent repo for this target under ATeam's home. Idempotent."""
    from ateam.intent import IntentError, adopt_home, init_intent

    paths = TargetPaths.from_root(target)
    moved = _previous_home(target) if not paths.exists() else None
    if moved:
        previous, old_home = moved
        if adopt:
            try:
                adopt_home(old_home, paths.intent.parent)
            except IntentError as e:
                _fail(str(e))
            click.echo(f"adopted {old_home} as {paths.intent.parent}")
        elif not fresh:
            _fail(f"this target's id changed from {previous['id']} ({previous['source']}) to {repo_id(target)} ({_derivation(target)}) "
                  f"and {old_home / 'intent'} still exists. Pass --adopt to move it to the new id, or --fresh to start a new intent repo")
    try:
        git = init_intent(paths, remote=remote)
    except IntentError as e:
        _fail(str(e))
    record_target(target)
    click.echo(f"intent repo {paths.intent} ({git.current_branch()}, {'remote ' + remote if remote else 'no remote'})")
    click.echo(f"repo id {repo_id(target)} derived from {_derivation(target)}")
    click.echo("nothing was written into the target repository")


@intent.command("path")
@TARGET_OPTION
def intent_path(target: Path) -> None:
    """Print the intent repo location."""
    click.echo(str(TargetPaths.from_root(target).intent))


@intent.command("remote")
@click.argument("url")
@TARGET_OPTION
def intent_remote(url: str, target: Path) -> None:
    """Set the private remote."""
    from ateam.intent import IntentError, set_remote

    try:
        set_remote(TargetPaths.from_root(target), url)
    except IntentError as e:
        _fail(str(e))
    click.echo(f"origin -> {url}")


@intent.command("push")
@TARGET_OPTION
def intent_push(target: Path) -> None:
    """Push the intent repo to its private remote."""
    from ateam.gitops import GitError
    from ateam.intent import IntentError, push

    try:
        click.echo(push(TargetPaths.from_root(target)).strip() or "pushed")
    except (IntentError, GitError) as e:
        _fail(str(e))


@intent.command("pull")
@TARGET_OPTION
def intent_pull(target: Path) -> None:
    """Fast-forward the intent repo from its private remote."""
    from ateam.gitops import GitError
    from ateam.intent import IntentError, pull

    _require_no_live_run_at(target)
    try:
        click.echo(pull(TargetPaths.from_root(target)).strip() or "pulled")
    except (IntentError, GitError) as e:
        _fail(str(e))


def _derivation(target: Path) -> str:
    url = origin_url(target)
    return f"origin {normalise_remote(url)}" if url else "path"


# ---------------------------------------------------------------- init (discovery)


@main.command()
@click.option("--remote", default=None, help="Private remote for the intent repo, when creating it.")
@click.option("--max-candidates", default=DEFAULT_MAX_CANDIDATES, show_default=True, type=int)
@click.option("--tier", default="careful", show_default=True, type=click.Choice(["light", "standard", "careful"]))
@TARGET_OPTION
def init(remote: str | None, max_candidates: int, tier: str, target: Path) -> None:
    """Discover a small baseline of records from the target and propose them for review."""
    import asyncio

    from ateam.contracts.store import load_contracts
    from ateam.discovery import candidates_to_records, render_discovery_report
    from ateam.intent import init_intent
    from ateam.plans.store import load_plans
    from ateam.records.store import load_records, save_record
    from ateam.roles import DISCOVERY_SPEC, Role
    from ateam.tiers import load_tiers

    _require_no_live_run_at(target)
    paths = TargetPaths.from_root(target)
    if not paths.exists():
        try:
            init_intent(paths, remote=remote)
        except IntentError as e:
            _fail(str(e))
        record_target(target)
    ctx = Context(target)
    records = load_records(ctx.paths.records)
    brief = assemble_investigation_brief(
        kind="discovery", target_root=ctx.paths.root, config=ctx.config, records=records,
        plans=load_plans(ctx.paths.plans), contracts=load_contracts(ctx.paths.contracts),
        scratch_dir=ctx.paths.scratch_root / "init", max_candidates=max_candidates,
    )
    tiers = load_tiers(ctx.config.tiers)
    result = asyncio.run(run_investigation(
        paths=ctx.paths, config=ctx.config, target_git=ctx.git, name="init", spec=DISCOVERY_SPEC,
        prompt=("Discover this repository's existing engineering intent. Read the Rules section of your brief first, "
                "then the code and history. Report candidates with evidence, and list unknowns."),
        brief_text=brief,
        tier=tiers[tier][Role.investigate], db=ctx.db, tier_name=tier,
    ))
    if result.tampered:
        _fail("discovery changed the checkout or the intent repo; report discarded: " + "; ".join(result.tampered))
    if result.res.report is None:
        _fail(f"discovery produced no report (subtype {result.res.subtype}{', ' + result.res.error if result.res.error else ''})")
    report = result.res.report
    disc = candidates_to_records(report.get("candidates", []), report.get("unknowns", []), records, ctx.paths.root,
                                 ctx.paths.records, cap_global=ctx.config.global_record_cap)
    for rec in disc.proposed:
        save_record(ctx.paths.records, rec)
    if disc.proposed:
        n = len(disc.proposed)
        commit_intent(ctx.paths, f"ateam: propose {n} record{'s' if n != 1 else ''} from discovery", ["records"])
    click.echo(render_discovery_report(disc))
    click.echo(f"\nsession {result.res.session_id}: {result.res.num_turns} turns, ${result.res.cost_usd:.4f}")


# ---------------------------------------------------------------- plan


def require_no_live_run(ctx) -> None:
    """Refuse (exit 4) while a run holds the lock: planning and running exclude each other (owner, 2026-10-26)."""
    from ateam.exclusion import run_holder

    held = run_holder(ctx.db)
    if held is None:
        return
    if held["alive"]:
        _fail(f"{held['plan_id']} is running (pid {held['pid']}); wait for it or stop it: ateam stop {held['plan_id']}", code=4)
    _fail(f"the run lock names {held['plan_id']} (pid {held['pid']}), a process that is gone; remove the lock by hand", code=4)


def require_no_live_planning(ctx) -> None:
    """Refuse (exit 4) while a planning session is live."""
    from ateam.exclusion import live_planning

    for plan_id, pid in live_planning(ctx.db):
        _fail(f"{plan_id} is being planned (pid {pid}); wait for it to finish", code=4)


def _require_no_live_run_at(target: Path) -> None:
    """The same guard for commands that have no Context: reads the store only when it already exists."""
    from types import SimpleNamespace

    state = home_dir(target) / "state.db"
    if state.exists():
        with StateDB(state) as db:
            db.init_schema()
            require_no_live_run(SimpleNamespace(db=db))


def _reject_stale_planning(ctx: Context) -> None:
    """Plans left at investigating by a planning process that is gone (killed, a closed terminal) become rejected with
    a committed plan file, so their id stays visible and nothing is stranded (owner, 2026-10-26). A plan whose marker
    names a live process is another terminal still planning and is left alone."""
    from datetime import UTC, datetime

    from ateam.plans.model import Plan, PlanHistoryEntry, PlanStatus
    from ateam.plans.store import load_plans, save_plan

    from ateam.gitops import GitError

    for fid in ctx.db.features_with_status("investigating"):
        marker = ctx.db.get_meta(f"planning:{fid}")
        if marker and marker.isdigit() and _process_alive(int(marker)):
            continue
        existing = load_plans(ctx.paths.plans).get(fid)
        files = sorted(ctx.paths.plans.glob(f"{fid}-*.yaml"))
        if existing is not None and str(existing.status) in ("plan_proposed", "plan_covered") and files:
            rel = intent_relpath(ctx.paths, files[0])
            git = GitRepo(ctx.paths.intent)
            try:
                committed = git.show_file("HEAD", rel) == files[0].read_text() and git.is_clean([rel])
            except GitError:
                committed = False
            if committed:   # the proposal was committed and only the store step was lost (owner, 2026-10-26)
                ctx.db.transition("feature", fid, str(existing.status), by="ateam", reason="completed from its committed plan file")
                click.echo(f"{fid} -> {existing.status} (completed from its committed plan file)")
                continue
        why = "interrupted (found stale)"
        requested = next((t["reason"] for t in ctx.db.transitions("feature", fid) if t["to_status"] == "requested"), None)
        plan = load_plans(ctx.paths.plans).get(fid) or Plan(id=fid, title=(requested or "interrupted planning")[:80],
                                                            request=requested or "unknown (interrupted before it was recorded)",
                                                            status=PlanStatus.plan_rejected)
        plan = plan.model_copy(update={"status": PlanStatus.plan_rejected, "history": [
            *plan.history, PlanHistoryEntry(at=datetime.now(UTC), by="ateam", to=PlanStatus.plan_rejected, why=why)]})
        try:
            require_clean(ctx.paths)
        except IntentError as e:
            click.echo(f"warning: could not record {fid} as rejected in the intent repo: {e}")
            continue
        written = save_plan(ctx.paths.plans, plan)
        message = f"ateam: reject plan {fid} (interrupted, found stale)"
        try:
            commit_intent_files(ctx.paths, message, [written])
        except (IntentError, GitError) as e:
            rel = intent_relpath(ctx.paths, written)
            _fail(f"could not commit the rejection of stale plan {fid}: {e}. Nothing else was run. Commit it by hand: "
                  f"git -C \"{ctx.paths.intent}\" add -- {rel} && git -C \"{ctx.paths.intent}\" commit -m \"{message}\" -- {rel}")
        ctx.db.transition("feature", fid, "plan_rejected", by="ateam", reason=why)
        click.echo(f"{fid} -> plan_rejected (interrupted, found stale)")


def _plan_contract_ids(ctx: Context, plan) -> list[str]:
    from ateam.plans.ops import plan_contract_ids

    return plan_contract_ids(ctx.db, plan)


def _started_reason(ctx: Context, plan, contracts: dict) -> str | None:
    """Why an approved plan can no longer be superseded, or None when none of its work has started (owner, 2026-10-26).
    Fails closed: any trace of work in the state store, or a file and store that disagree, counts as started."""
    store_status = ctx.db.feature_status(plan.id) or "missing"
    if plan.status != "plan_approved" or store_status != "plan_approved":
        return f"{plan.id} is {plan.status} in its file and {store_status} in the state store"
    rows = {r["contract_id"]: r for r in ctx.db.contracts_for_plan(plan.id)}
    for cid in _plan_contract_ids(ctx, plan):
        c = contracts.get(cid)
        if c is None:
            return f"{cid} has no contract file"
        if c.plan != plan.id:
            return f"{cid} belongs to {c.plan} in its file"
        if cid not in rows:
            stored = ctx.db.contract_status(cid)
            return f"{cid} has no row in the state store" if stored is None else f"{cid} belongs to another plan in the state store"
        traces = ctx.db.work_traces(cid)
        if traces:
            return f"{cid} has {traces[0]}"
        if c.status not in ("draft", "ready") or rows[cid]["status"] not in ("draft", "ready"):
            return f"{cid} is {c.status} in its file and {rows[cid]['status']} in the state store"
    return None


@main.command()
@click.argument("request")
@click.option("--tier", default="careful", show_default=True, type=click.Choice(["light", "standard", "careful"]))
@click.option("--replaces", default=None, metavar="P-xxxx", help="Supersede this plan when the new plan is approved: a plan_proposed plan, or an approved one none of whose contracts has started.")
@click.option("--no-review", "no_review", is_flag=True, help="Skip the staff-engineer review of the draft plan.")
@TARGET_OPTION
def plan(request: str, tier: str, replaces: str | None, no_review: bool, target: Path) -> None:
    """Turn a feature request into contracts and a plan for approval."""
    import asyncio
    import os

    from ateam.contracts.store import load_contracts, save_contract
    from ateam.brief import assemble_review_brief
    from ateam.planner import PlanError, format_finding, render_draft, render_plan_review, report_to_plan
    from ateam.plans.store import load_plans
    from ateam.plans.store import next_id as next_plan_id
    from ateam.plans.store import save_plan
    from ateam.records.store import load_records, save_record
    from ateam.roles import PLANNER_SPEC, REVIEWER_SPEC, Role
    from ateam.tiers import load_tiers

    from datetime import UTC, datetime

    from pydantic import ValidationError

    from ateam.gitops import GitError
    from ateam.plans.model import Plan, PlanHistoryEntry, PlanReview, PlanStatus

    ctx = _context(target)
    require_no_live_run(ctx)
    if replaces is not None:
        old = load_plans(ctx.paths.plans).get(replaces)
        if old is None:
            _fail(f"--replaces names unknown plan {replaces}")
        if old.status == PlanStatus.plan_approved:
            why = _started_reason(ctx, old, load_contracts(ctx.paths.contracts))
        else:
            why = None if old.status == PlanStatus.plan_proposed else f"{replaces} is {old.status}"
        if why:
            _fail(f"--replaces needs a plan_proposed plan or an approved plan that has not started; {why}")
    _reject_stale_planning(ctx)
    disk_next = int(next_plan_id(ctx.paths.plans).split("-")[1])
    db_max = ctx.db.max_feature_id()
    db_next = int(db_max.split("-")[1]) + 1 if db_max else 1
    pid = f"P-{max(disk_next, db_next):04d}"
    ctx.db.set_meta(f"planning:{pid}", str(os.getpid()))   # first, so a concurrent cleanup never sees this plan unmarked
    ctx.db.transition("feature", pid, "requested", by="owner", reason=request[:200])
    ctx.db.transition("feature", pid, "investigating", by="ateam")
    def _manual(files: list[Path], message: str) -> str:
        """The command that commits exactly `files` by hand; the pathspec on `commit` keeps anything else staged out."""
        intent = ctx.paths.intent
        names = " ".join(intent_relpath(ctx.paths, f) for f in files)
        return f"git -C \"{intent}\" add -- {names} && git -C \"{intent}\" commit -m \"{message}\" -- {names}"

    def _uncommitted(session: str, files: list[Path], message: str) -> None:
        """A session changed the intent repo: the files stay written for the owner to inspect, but no ateam: commit may carry a planted file."""
        click.echo(f"warning: the {session} session changed the intent repo; nothing was committed. Inspect: git -C \"{ctx.paths.intent}\" status, "
                   f"then commit the plan by hand: {_manual(files, message)}")

    def _rejected(reason: str, tampered: list[str] | None = None) -> None:
        """Leave a visible, terminal trace: a rejected plan file (committed unless the intent repo was changed) and the feature state."""
        rejected = Plan(id=pid, title=request[:80], request=request, status=PlanStatus.plan_rejected,
                        replaces=replaces, history=[PlanHistoryEntry(at=datetime.now(UTC), by="ateam", to=PlanStatus.plan_rejected, why=reason)])
        written = save_plan(ctx.paths.plans, rejected)
        message = f"ateam: reject plan {pid} ({reason})"
        if tampered and INTENT_TAMPER in tampered:
            _uncommitted("planner", [written], message)
        else:
            try:
                commit_intent_files(ctx.paths, message, [written])
            except (IntentError, GitError) as e:
                click.echo(f"warning: could not commit the rejected plan in the intent repo: {e}; commit it by hand: {_manual([written], message)}")
        if ctx.db.feature_status(pid) in ("investigating", "plan_proposed"):
            ctx.db.transition("feature", pid, "plan_rejected", by="ateam", reason=reason)

    try:
        records = load_records(ctx.paths.records)
        tiers = load_tiers(ctx.config.tiers)
        existing_plans = load_plans(ctx.paths.plans)
        brief = assemble_investigation_brief(
            kind="planning", target_root=ctx.paths.root, config=ctx.config, records=records,
            plans=existing_plans, contracts=load_contracts(ctx.paths.contracts),
            scratch_dir=ctx.paths.scratch_root / pid, request=request,
            exclude_plans=frozenset({replaces}) if replaces else frozenset(),
        )
    except KeyboardInterrupt:
        _rejected("interrupted")
        click.echo(f"interrupted: {pid} -> plan_rejected; re-run: ateam plan \"{request}\"")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001  the feature is already investigating; never strand it
        _rejected(f"brief assembly failed: {type(e).__name__}")
        _fail(f"could not assemble the planning brief: {e}. Re-run: ateam plan \"{request}\"")

    try:
        result = asyncio.run(run_investigation(
            paths=ctx.paths, config=ctx.config, target_git=ctx.git, name=pid, spec=PLANNER_SPEC,
            prompt=("Produce the plan for the Request in your brief. Read the Rules section first; anything it reserves "
                    "for the owner goes under unknowns, not into a contract. Fill every field of the report: contracts, "
                    "proposed_records, depends_on_plans, files_read, out_of_scope, verification_approach, unknowns, notes."),
            brief_text=brief,
            tier=tiers[tier][Role.investigate], db=ctx.db, tier_name=tier,
        ))
    except KeyboardInterrupt:
        _rejected("interrupted")   # never stranded at investigating (owner, 2026-10-26)
        click.echo(f"interrupted: {pid} -> plan_rejected; re-run: ateam plan \"{request}\"")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001  the session is the untrusted part; never leave the plan half-open
        _rejected(f"planner session failed: {type(e).__name__}")
        _fail(f"planning failed before any report: {e}. Re-run: ateam plan \"{request}\"")

    if result.tampered:
        _rejected("planner changed the checkout or intent repo", tampered=result.tampered)
        _fail("planning changed the checkout or the intent repo; report discarded: " + "; ".join(result.tampered))
    if result.res.report is None:
        _rejected("planner produced no report")
        _fail(f"planning produced no report (subtype {result.res.subtype}{', ' + result.res.error if result.res.error else ''})")
    try:
        pr = report_to_plan(result.res.report, request, pid, ctx.paths.contracts, records, ctx.config, ctx.paths.root, ctx.paths.records,
                            tiers=tiers, existing_plans=existing_plans, replaces=replaces)
    except KeyboardInterrupt:
        _rejected("interrupted")
        click.echo(f"interrupted: {pid} -> plan_rejected; re-run: ateam plan \"{request}\"")
        sys.exit(130)
    except PlanError as e:
        _rejected("planner output invalid")
        click.echo("The planner's output is not usable; no contracts were written. Problems:")
        for problem in e.problems:
            click.echo(f"  - {problem}")
        click.echo(f"Re-run: ateam plan \"{request}\"")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001  a report the planner code could not read; never strand the plan
        _rejected(f"planner output could not be read: {type(e).__name__}")
        _fail(f"the planner's report could not be read ({type(e).__name__}: {e}); no contracts were written. "
              f"Re-run: ateam plan \"{request}\"")
    sessions = [result.res]
    intent_tampered_by: str | None = None   # the first later session that changed the intent repo
    proposal = {"committed": True}          # False when the proposal commit failed: the plan is written, the run exits 1

    def _propose(pr, tampered_by: str | None) -> None:
        """Write the plan, its contracts and records; commit exactly those files unless a session changed the intent repo."""
        written = [save_contract(ctx.paths.contracts, c) for c in pr.contracts]
        written += [save_record(ctx.paths.records, r) for r in pr.proposed.proposed]
        written.append(save_plan(ctx.paths.plans, pr.plan.model_copy(update={"replaces": replaces})))
        message = f"ateam: propose plan {pid}"
        if tampered_by:
            _uncommitted(tampered_by, written, message)
        else:
            try:
                commit_intent_files(ctx.paths, message, written)
            except (IntentError, GitError) as e:
                click.echo(f"warning: could not commit the plan in the intent repo: {e}; nothing was committed. "
                           f"Commit it by hand: {_manual(written, message)}")
                proposal["committed"] = False
        ctx.db.transition("feature", pid, "plan_proposed", by="ateam")

    def _with_note(notes: str, note: str) -> str:
        return f"{notes}\n{note}" if notes else note

    def _review_and_revise(pr, progress: dict):
        """Run the review and at most one revision. Returns (plan result, review); never raises an ordinary Exception."""
        nonlocal intent_tampered_by
        exclude = frozenset({replaces}) if replaces else frozenset()
        review: PlanReview | None = None
        reason = ""
        review_res = None
        try:
            review_brief = assemble_review_brief(
                target_root=ctx.paths.root, config=ctx.config, records=records, plans=existing_plans,
                contracts=load_contracts(ctx.paths.contracts), scratch_dir=ctx.paths.scratch_root / f"{pid}-review",
                request=request, draft=render_draft(pr), exclude_plans=exclude,
            )
            reviewed = asyncio.run(run_investigation(
                paths=ctx.paths, config=ctx.config, target_git=ctx.git, name=f"{pid}-review", spec=REVIEWER_SPEC,
                prompt=("Review the Draft plan in your brief against the Request. Read the Rules section first and open the "
                        "files the contracts name. Fill every field of the report: verdict, findings, notes."),
                brief_text=review_brief, tier=tiers[tier][Role.investigate], db=ctx.db, tier_name=tier,
            ))
        except Exception as e:  # noqa: BLE001  the review session is untrusted; it can never cost the owner the plan
            reason = f"the session failed: {type(e).__name__}: {e}"
        else:
            review_res = reviewed.res
            sessions.append(review_res)
            report = review_res.report
            if reviewed.tampered:
                reason = "repository state changed during review: " + "; ".join(reviewed.tampered)
                if INTENT_TAMPER in reviewed.tampered:
                    intent_tampered_by = "review"
            elif report is None:
                reason = f"no report, subtype {review_res.subtype}"
                if review_res.terminal_reason:
                    reason += f", terminal reason {review_res.terminal_reason}"
                if review_res.error:
                    reason += f", {review_res.error}"
            elif not isinstance(report, dict) or report.get("verdict") not in ("defensible", "needs_revision"):
                reason = "the report is not a review"
            else:
                try:
                    review = PlanReview.model_validate({
                        "verdict": report["verdict"], "findings": report.get("findings") or [], "notes": report.get("notes") or "",
                        "session_id": review_res.session_id, "cost_usd": review_res.cost_usd,
                    })
                except ValidationError:
                    reason = "the report does not match the review schema"
        if review is None:
            click.echo(f"warning: review unavailable ({reason}); the plan is unreviewed")
            review = PlanReview(verdict="unavailable", notes=reason, session_id=review_res.session_id if review_res else None,
                                cost_usd=review_res.cost_usd if review_res else 0.0)
        progress["review"] = review
        # one revision round for blocking or important findings (owner, 2026-10-26); minor ones travel with the plan
        if review.verdict != "needs_revision" or not any(f.severity in ("blocking", "important") for f in review.findings):
            return pr, review
        revision_note = ""
        prompt = ("A staff-engineer review found problems with your plan. Revise the plan to address every "
                  "blocking and important finding below, keep what was right, and return the complete report again.\n\n"
                  + "\n".join(f"- {format_finding(f)}" for f in review.findings))
        try:
            if not result.res.session_id:
                raise RuntimeError("the planner session has no id to resume")
            revised = asyncio.run(run_investigation(
                paths=ctx.paths, config=ctx.config, target_git=ctx.git, name=pid, spec=PLANNER_SPEC, prompt=prompt,
                brief_text=brief, tier=tiers[tier][Role.investigate], db=ctx.db, tier_name=tier,
                resume=result.res.session_id,
            ))
        except Exception as e:  # noqa: BLE001  a failed revision keeps the first valid plan
            revision_note = f"the session failed: {type(e).__name__}: {e}"
        else:
            sessions.append(revised.res)
            if revised.tampered:
                revision_note = "the session changed the checkout or intent repo: " + "; ".join(revised.tampered)
                if INTENT_TAMPER in revised.tampered:
                    intent_tampered_by = intent_tampered_by or "revision"
            elif revised.res.report is None:
                revision_note = f"no report, subtype {revised.res.subtype}"
            else:
                try:
                    pr2 = report_to_plan(revised.res.report, request, pid, ctx.paths.contracts, records, ctx.config,
                                         ctx.paths.root, ctx.paths.records, tiers=tiers, existing_plans=existing_plans,
                                         replaces=replaces)
                except PlanError as e:
                    revision_note = "the revised report is not usable: " + "; ".join(e.problems)
                else:
                    if not pr2.covered:
                        return pr2, review.model_copy(update={"revised": True})
                    revision_note = (f"the revised report calls the request covered by {', '.join(pr2.plan.covered_by)} "
                                     "instead of revising the plan")
        pr.problems.append(f"the planner's revision was discarded ({revision_note}); kept the first plan")
        click.echo(f"warning: revision discarded ({revision_note}); kept the first plan")
        return pr, review.model_copy(update={"notes": _with_note(review.notes, f"revision discarded: {revision_note}")})

    if not pr.covered and not no_review:
        progress: dict = {"review": None}
        try:
            pr, review = _review_and_revise(pr, progress)
        except BaseException:
            # Interrupted (Ctrl-C, cancellation): keep the validated first plan, unreviewed or with its review, then stop.
            done = progress["review"]
            if done is None:
                kept = PlanReview(verdict="unavailable", notes="review interrupted")
            else:
                kept = done.model_copy(update={"notes": _with_note(done.notes, "revision discarded: interrupted")})
            pr.plan = pr.plan.model_copy(update={"review": kept})
            try:
                _propose(pr, intent_tampered_by)
                click.echo(f"interrupted: {pid} was saved as plan_proposed with review {kept.verdict}")
            except Exception as e:  # noqa: BLE001  never mask the interrupt
                click.echo(f"warning: could not save {pid} after the interrupt: {e}")
            raise
        pr.plan = pr.plan.model_copy(update={"review": review})

    def _session_lines() -> None:
        click.echo("")
        for res in sessions:
            click.echo(f"session {res.session_id}: {res.num_turns} turns, ${res.cost_usd:.4f}")
        if len(sessions) > 1:
            click.echo(f"total cost: ${sum(res.cost_usd for res in sessions):.4f} across {len(sessions)} sessions")

    if pr.covered:
        covered_file = save_plan(ctx.paths.plans, pr.plan)
        covered_msg = f"ateam: plan {pid} covered by {', '.join(pr.plan.covered_by)}"
        try:
            commit_intent_files(ctx.paths, covered_msg, [covered_file])
        except (IntentError, GitError) as e:
            click.echo(f"warning: could not commit the covered plan in the intent repo: {e}; commit it by hand: {_manual([covered_file], covered_msg)}")
        ctx.db.transition("feature", pid, "plan_covered", by="ateam", reason=f"covered by {', '.join(pr.plan.covered_by)}")
        if replaces:
            click.echo(f"{replaces} is not superseded; only approval supersedes")
        click.echo(render_plan_review(pr, tiers))
        _session_lines()
        return
    _propose(pr, intent_tampered_by)
    click.echo(render_plan_review(pr, tiers))
    _session_lines()
    if not proposal["committed"]:
        sys.exit(1)


def _dependency_order(contracts: dict) -> list[str]:
    from ateam.plans.ops import dependency_order

    return dependency_order(contracts)


def _parse_tier_overrides(specs: tuple[str, ...], plan_contracts: list[str], tier_names: list[str]) -> dict[str, dict[str, str]]:
    overrides: dict[str, dict[str, str]] = {}
    for spec in specs:
        if "=" not in spec:
            _fail(f"bad --tier {spec!r}; use C-0002=careful or C-0002:test=careful")
        lhs, name = spec.split("=", 1)
        cid, _, role = lhs.partition(":")
        if cid not in plan_contracts:
            _fail(f"--tier {spec!r}: {cid} is not in this plan ({', '.join(plan_contracts)})")
        if name not in tier_names:
            _fail(f"--tier {spec!r}: unknown tier {name!r}; known: {', '.join(tier_names)}")
        roles = [role] if role else ["build", "test"]   # investigate changes only when named; "none" is a real setting
        if any(r not in ("build", "test", "investigate") for r in roles):
            _fail(f"--tier {spec!r}: role must be build, test or investigate")
        overrides.setdefault(cid, {}).update({r: name for r in roles})
    return overrides


@main.command()
@click.argument("plan_id")
@click.option("--tier", "tier_specs", multiple=True, help="Override tiers: C-0002=careful (build and test) or C-0002:investigate=light (one role); repeatable.")
@click.option("--skip-records", is_flag=True, help="Do not walk the plan's proposed records now.")
@click.option("--force", is_flag=True, help="Approve a replacement whose replaced plan is rejected, covered or gone; nothing is superseded. Never overrides an approved plan that has started.")
@TARGET_OPTION
def approve(plan_id: str, tier_specs: tuple[str, ...], skip_records: bool, force: bool, target: Path) -> None:
    """Approve a plan: review its proposed records, apply tier overrides, freeze its contracts."""
    from datetime import UTC, datetime

    from ateam.allocation import investigate_tier_for, tier_for_complexity, tier_for_test
    from ateam.contracts.model import ContractStatus, Level
    from ateam.contracts.select import freeze, select_records
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.gitops import GitError
    from ateam.plans.model import PlanHistoryEntry, PlanStatus
    from ateam.plans.store import load_plans, save_plan
    from ateam.records.graph import RecordGraph
    from ateam.records.store import load_records
    from ateam.state.db import InvalidTransition
    from ateam.tiers import ORDER, at_least, load_tiers

    ctx = _context(target)
    require_no_live_run(ctx)
    plans_ = load_plans(ctx.paths.plans)
    if plan_id not in plans_:
        _fail(f"unknown plan {plan_id}")
    plan_ = plans_[plan_id]
    if plan_.status != PlanStatus.plan_proposed:
        _fail(f"{plan_id} is {plan_.status}; only plan_proposed plans can be approved")
    replacer = next((p for p in plans_.values() if p.replaces == plan_id and p.status == PlanStatus.plan_proposed), None)
    if replacer is not None:
        _fail(f"{plan_id} is replaced by proposed plan {replacer.id}; approve or reject {replacer.id} first")
    # The replaced plan is superseded when it is plan_proposed in both the file and the state store, or approved in both
    # with none of its work started. An approved plan that has started is never left live beside its replacement.
    supersede = False
    old_approved = False
    old = plans_.get(plan_.replaces) if plan_.replaces else None

    def refuse_if_started() -> None:
        why = _started_reason(ctx, old, load_contracts(ctx.paths.contracts)) if old is not None else f"{plan_.replaces} has no plan file"
        if why:
            _fail(f"{plan_.replaces} cannot be superseded ({why}); reject {plan_id} or finish {plan_.replaces}")

    if plan_.replaces:
        file_status = str(old.status) if old is not None else "missing"
        store_status = ctx.db.feature_status(plan_.replaces) or "missing"
        if file_status == "plan_proposed" and store_status == "plan_proposed":
            supersede = True
        elif "plan_approved" in (file_status, store_status):
            refuse_if_started()
            supersede = old_approved = True
        elif not force:
            shown = store_status if store_status != "plan_proposed" else file_status
            _fail(f"{plan_.replaces} is {shown}; approving {plan_id} would leave two live plans for the same request; "
                  f"pass --force to approve without superseding")
    tiers = load_tiers(ctx.config.tiers)
    overrides = _parse_tier_overrides(tier_specs, plan_.contracts, list(tiers))

    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    if plan_.proposed_records and not skip_records:
        click.echo(f"Records proposed by {plan_id}:")
        _review_records(ctx, list(plan_.proposed_records), counts)
        click.echo(f"records: approved {counts['approved']}, rejected {counts['rejected']}, skipped {counts['skipped']}")
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        _fail(str(e))
    if old_approved:
        refuse_if_started()   # again: the record review can wait on prompts, and worker run and verify take no lock

    cs = load_contracts(ctx.paths.contracts)
    missing = [cid for cid in plan_.contracts if cid not in cs]
    if missing:
        _fail(f"{plan_id} names contracts that do not exist: {', '.join(missing)}")
    records = load_records(ctx.paths.records)
    graph = RecordGraph(records)
    notes: list[str] = []
    frozen_all: list[tuple] = []
    approval_files: list[Path] = []   # exactly what this approval writes; nothing else rides the commit
    click.echo("Frozen contracts, in dependency order:")
    for cid in _dependency_order({cid: cs[cid] for cid in plan_.contracts}):
        c = cs[cid]
        tier = dict(c.allocation.tier)
        tier.update(overrides.get(cid, {}))
        build = tier.get("build") if tier.get("build") in ORDER else "standard"
        wanted = at_least(build, tier.get("test") if tier.get("test") in ORDER else "light")
        if wanted != tier.get("test"):
            notes.append(f"{cid}: test tier raised from {tier.get('test')!r} to {wanted!r} (TEST never runs below BUILD)")
            tier["test"] = wanted
        if tier.get("investigate", "none") == "none" and Level(c.allocation.uncertainty) in (Level.medium, Level.high):
            tier["investigate"] = investigate_tier_for(c.allocation.uncertainty)
            notes.append(f"{cid}: investigate tier set to {tier['investigate']!r} for {c.allocation.uncertainty} uncertainty")
        if cid in overrides:
            build_floor = tier_for_complexity(c.allocation.complexity)
            if ORDER.index(tier["build"]) < ORDER.index(build_floor):
                notes.append(f"{cid}: build tier {tier['build']!r} is below the rubric floor {build_floor!r} ({c.allocation.complexity} complexity); owner override kept")
            test_floor = tier_for_test(c.allocation.risk, tier["build"])
            if ORDER.index(tier["test"]) < ORDER.index(test_floor):
                notes.append(f"{cid}: test tier {tier['test']!r} is below the rubric floor {test_floor!r} ({c.allocation.risk} risk); owner override kept")
        c = c.model_copy(update={"allocation": c.allocation.model_copy(update={"tier": tier})})
        selected = select_records(c.paths, graph, ctx.paths.root)
        frozen = freeze(c, selected)
        approval_files.append(save_contract(ctx.paths.contracts, frozen))
        frozen_all.append((frozen, selected))
        click.echo(f"  {frozen.id}  {frozen.status:<6} build={tier['build']} test={tier['test']} investigate={tier.get('investigate', 'none')}  records: {', '.join(r.id for r in selected) or 'none'}")
    for n in notes:
        click.echo(f"  note: {n}")
    plan_ = plan_.model_copy(update={
        "status": PlanStatus.plan_approved, "tier_overrides": overrides,
        "history": [*plan_.history, PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.plan_approved)],
    })
    approval_files.append(save_plan(ctx.paths.plans, plan_))
    superseded_id = None
    old_steps: dict[str, str] = {}   # an approved predecessor's contracts and their store status before the commit
    if old is not None and supersede:
        superseded_id = old.id
        approval_files.append(save_plan(ctx.paths.plans, old.model_copy(update={
            "status": PlanStatus.superseded,
            "history": [*old.history, PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.superseded, why=f"replaced by {plan_id}")],
        })))
        if old_approved:
            for cid in _plan_contract_ids(ctx, old):
                old_steps[cid] = ctx.db.contract_status(cid)
                approval_files.append(save_contract(ctx.paths.contracts, cs[cid].model_copy(update={"status": ContractStatus.superseded})))
    try:
        commit_intent_files(ctx.paths, f"ateam: approve plan {plan_id}", approval_files)
    except (IntentError, GitError) as e:
        click.echo(f"error: could not commit the approval in the intent repo: {e}")
        click.echo("Nothing was recorded in the state store. Restore the intent repo and re-run:")
        click.echo(f"  git -C \"{ctx.paths.intent}\" checkout -- . && git -C \"{ctx.paths.intent}\" clean -fd plans contracts")
        sys.exit(1)
    # The intent repo is the truth; only now does the state store follow it.
    for frozen, selected in frozen_all:
        ctx.db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
        for r in selected:
            ctx.db.set_record_hash(r.id, r.content_hash())
    ctx.db.transition("feature", plan_id, "plan_approved", by="owner")
    click.echo(f"{plan_id} -> plan_approved")
    _echo_lanes(ctx, plan_)
    if superseded_id is None:
        return
    if old_approved:
        try:
            ctx.db.supersede(superseded_id, "plan_approved", old_steps, by="owner", reason=f"replaced by {plan_id}")
        except InvalidTransition as e:
            click.echo(f"error: the intent repo says {', '.join([superseded_id, *old_steps])} are superseded but the state store "
                       f"was not changed: {e}. Investigate before running any of them.")
            sys.exit(1)
        for cid in old_steps:
            click.echo(f"{cid} -> superseded")
        click.echo(f"{superseded_id} -> superseded")
    elif ctx.db.feature_status(superseded_id) == "plan_proposed":
        ctx.db.transition("feature", superseded_id, "superseded", by="owner", reason=f"replaced by {plan_id}")
        click.echo(f"{superseded_id} -> superseded")
    for other in sorted(plans_.values(), key=lambda p: p.id):
        for field in ("covered_by", "depends_on_plans"):
            if superseded_id in getattr(other, field) and other.id not in (plan_id, superseded_id):
                click.echo(f"note: {other.id} names superseded {superseded_id} in {field}")


@main.command()
@click.argument("plan_id")
@click.argument("reason")
@TARGET_OPTION
def reject(plan_id: str, reason: str, target: Path) -> None:
    """Reject a proposed plan (its contracts stay draft and are never frozen), a merge proposal (the integration
    branch and the target are left as they are), or an approved plan none of whose work has started (its contracts
    are superseded and never run)."""
    from datetime import UTC, datetime

    from ateam.plans.model import PlanHistoryEntry, PlanStatus
    from ateam.plans.store import load_plans, save_plan

    ctx = _context(target)
    require_no_live_run(ctx)
    plans_ = load_plans(ctx.paths.plans)
    if plan_id not in plans_:
        _fail(f"unknown plan {plan_id}")
    plan_ = plans_[plan_id]
    stored = ctx.db.feature_status(plan_id)
    if plan_.status == PlanStatus.plan_approved:
        _reject_approved(ctx, plan_, reason)
        return
    if plan_.status not in (PlanStatus.plan_proposed, PlanStatus.merge_proposed) or (
            plan_.status == PlanStatus.merge_proposed and stored != "merge_proposed"):
        _fail(f"{plan_id} is {plan_.status} in its file and {stored} in the state store; "
              "only plan_proposed, merge_proposed or unstarted approved plans can be rejected")
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        _fail(str(e))
    plan_ = plan_.model_copy(update={
        "status": PlanStatus.plan_rejected,
        "history": [*plan_.history, PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.plan_rejected, why=reason)],
    })
    commit_intent_files(ctx.paths, f"ateam: reject plan {plan_id}", [save_plan(ctx.paths.plans, plan_)])
    ctx.db.transition("feature", plan_id, "plan_rejected", by="owner", reason=reason)
    click.echo(f"{plan_id} -> plan_rejected")


def _reject_approved(ctx: Context, plan_, reason: str) -> None:
    """An approved plan that has not started (owner, 2026-10-26): the plan file, its contracts as superseded, one intent
    commit, then the store in one transaction."""
    from datetime import UTC, datetime

    from ateam.contracts.model import ContractStatus
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.plans.model import PlanHistoryEntry, PlanStatus
    from ateam.plans.store import save_plan
    from ateam.state.db import InvalidTransition

    cs = load_contracts(ctx.paths.contracts)
    why = _started_reason(ctx, plan_, cs)
    if why:
        _fail(f"{plan_.id} cannot be rejected ({why}); only an approved plan that has not started can be rejected")
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        _fail(str(e))
    steps = {cid: ctx.db.contract_status(cid) for cid in _plan_contract_ids(ctx, plan_)}
    files = [save_plan(ctx.paths.plans, plan_.model_copy(update={
        "status": PlanStatus.plan_rejected,
        "history": [*plan_.history, PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.plan_rejected, why=reason)],
    }))]
    files += [save_contract(ctx.paths.contracts, cs[cid].model_copy(update={"status": ContractStatus.superseded})) for cid in steps]
    commit_intent_files(ctx.paths, f"ateam: reject plan {plan_.id}", files)
    try:
        ctx.db.retire(plan_.id, "plan_approved", steps, by="owner", reason=reason, to="plan_rejected")
    except InvalidTransition as e:
        _fail(f"the intent repo says {', '.join([plan_.id, *steps])} are rejected but the state store was not changed: {e}. "
              "Investigate before running any of them.")
    for cid in steps:
        click.echo(f"{cid} -> superseded")
    click.echo(f"{plan_.id} -> plan_rejected")


@main.command()
@click.argument("plan_id")
@RESULT_FILE_OPTION
@TARGET_OPTION
def integrate(plan_id: str, result_file: Path | None, target: Path) -> None:
    """Merge a plan's verified contracts onto ateam/<plan-id>, run the full checks there and propose the merge.

    Exit 0 merge_proposed, 1 checks or setup failed (paused), 3 merge conflict (paused), 4 refused."""
    from ateam.gitops import GitError
    from ateam.integrate import IntegrateRefused
    from ateam.integrate import integrate as run_integrate

    with _Result(result_file, {"outcome": "refused", "proposal": None, "reasons": []}) as out:
        ctx = _context(target)
        try:
            res = run_integrate(ctx, plan_id, echo=click.echo)
        except IntegrateRefused as e:
            _fail(str(e), code=4)
        except (GitError, IntentError) as e:
            out["outcome"] = "failed"
            _fail(f"git failed during integration: {e}. Re-run ateam integrate {plan_id} once it is fixed; "
                  "completed steps are kept")
        out.update(outcome=res.outcome, proposal=str(res.proposal) if res.proposal else None, reasons=list(res.messages))
        for m in res.messages:
            click.echo(m)
        if res.outcome == "merge_proposed":
            click.echo(f"Review {res.proposal}, then: ateam merge {plan_id}   (or: ateam reject {plan_id} \"reason\")")
            sys.exit(0)
        sys.exit(3 if res.outcome == "conflict" else 1)


def _release_own_run_lock(target: Path, plan_id: str) -> None:
    """Release the run lock only when this process holds it for this plan (a second Ctrl-C; owner, 2026-10-26)."""
    import os

    with StateDB(home_dir(target) / "state.db") as db:
        db.init_schema()
        info = db.run_lock_info()
        if info and info["plan_id"] == plan_id and info["pid"] == os.getpid():
            db.release_run_lock(plan_id)


def _interrupt_handler(stop, say):
    """The first Ctrl-C asks the scheduler to stop after the running steps; a second one exits at once."""

    def handle(signum, frame) -> None:
        if stop.is_set():
            raise SystemExit(130)
        stop.set()
        say("stopping after the running steps; Ctrl-C again to exit now")

    return handle


@main.command("run")
@click.argument("plan_id")
@click.option("--dry-run", is_flag=True, help="Print the schedule and the worst-case cost; launch and write nothing.")
@TARGET_OPTION
def run_cmd(plan_id: str, dry_run: bool, target: Path) -> None:
    """Execute an approved plan until the merge gate or a pause. Never merges into the target branch.

    Exit 0 merge_proposed (or a dry run), 1 paused, stalled or out of run budget, 4 refused or a re-run is needed, 130 interrupted."""
    _run_foreground(target, plan_id, dry_run)


def _run_foreground(target: Path, plan_id: str, dry_run: bool) -> NoReturn:
    """Run the scheduler in a worker thread with the Ctrl-C handler in the main thread; exits with run's code.
    Shared by `ateam run` and `ateam resume`."""
    import signal
    import threading

    from ateam import scheduler

    _context(target)   # exit 2 when the target has no intent repo
    stop = threading.Event()
    box: dict = {}

    def work() -> None:
        try:
            box["summary"] = scheduler.run_plan(lambda: Context(target), plan_id, click.echo, stop,
                                                launch=scheduler.default_launch, dry_run=dry_run,
                                                investigate=scheduler.default_investigate)
        except BaseException as e:  # noqa: BLE001  (reported below, in the main thread)
            box["error"] = e

    in_main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, _interrupt_handler(stop, click.echo)) if in_main else None
    worker = threading.Thread(target=work, name="ateam-run", daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.2)
    except SystemExit:
        _release_own_run_lock(target, plan_id)   # the scheduler thread dies with us; its finally never runs
        raise
    finally:
        if in_main:
            signal.signal(signal.SIGINT, previous)
    if "error" in box:
        if isinstance(box["error"], scheduler.RunRefused):
            _fail(str(box["error"]), code=4)
        raise box["error"]
    summary = box["summary"]
    for m in summary.messages:
        if summary.outcome not in ("merge_proposed", "dry_run"):
            click.echo(m)
    if summary.outcome == "merge_proposed":
        click.echo(f"{plan_id} -> merge_proposed. Review {summary.proposal}, then: ateam merge {plan_id}   "
                   f"(or: ateam reject {plan_id} \"reason\")")
    sys.exit({"merge_proposed": 0, "dry_run": 0, "paused": 1, "stalled": 1, "budget": 1, "rerun": 4, "interrupted": 130}[summary.outcome])


@main.command("stop")
@click.argument("plan_id")
@TARGET_OPTION
def stop_cmd(plan_id: str, target: Path) -> None:
    """Pause an executing plan. A live run is asked through the state store and pauses after interrupting its own
    sessions; with no run, the plan is paused at once (reason owner_stopped). Exit 0 requested or paused, 4 refused."""
    from ateam.exclusion import run_holder
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans

    ctx = _context(target)
    plan = load_plans(ctx.paths.plans).get(plan_id)
    if plan is None:
        _fail(f"unknown plan {plan_id}", code=4)
    status = ctx.db.feature_status(plan_id) or str(plan.status)
    if status != "executing":
        _fail(f"{plan_id} is {status}; only an executing plan can be stopped", code=4)
    held = run_holder(ctx.db)
    if held is not None and held["plan_id"] != plan_id:
        require_no_live_run(ctx)   # names the other plan's run (or its stale lock)
    if held is not None and not held["alive"]:
        _fail(f"the run lock names {plan_id} (pid {held['pid']}), a process that is gone; remove the lock by hand first", code=4)
    if held is not None:
        if ctx.db.stop_requested(plan_id):
            _fail(f"a stop is already in progress for {plan_id}", code=4)
        ctx.db.request_stop(plan_id)   # the run polls the store and interrupts only its own sessions
        click.echo(f"stop requested; {plan_id} pauses after its running sessions are interrupted")
        sys.exit(0)
    require_no_live_planning(ctx)
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        _fail(str(e), code=4)
    if not ctx.db.acquire_run_lock(plan_id):   # a run started a moment ago: ask it instead
        _fail(f"{plan_id} just started running; run ateam stop {plan_id} again", code=4)
    try:
        why = "owner_stopped: stopped by the owner"
        set_plan_status(ctx.paths, ctx.db, plan, ["paused"], by="owner", why=why)
        ctx.db.record_pause(plan_id, "owner_stopped", "stopped by the owner", [])
    finally:
        ctx.db.release_run_lock(plan_id)
    click.echo(f"{plan_id} -> paused (owner_stopped)")


@main.command("resume")
@click.argument("plan_id")
@click.option("--adopt", is_flag=True, help="Use each paused contract's investigation suggestion as your instruction.")
@click.option("--note", default=None, help="Your instruction for the next BUILD (guidance within the frozen contract).")
@click.option("--note-file", type=click.Path(dir_okay=False, exists=True, path_type=Path), default=None,
              help="Read the instruction from a file.")
@click.option("--contract", "contract_ids", multiple=True, help="Which contract an instruction is for (repeatable).")
@click.option("--no-run", is_flag=True, help="Only resume the plan's state; do not continue the run.")
@TARGET_OPTION
def resume(plan_id: str, adopt: bool, note: str | None, note_file: Path | None, contract_ids: tuple[str, ...], no_run: bool,
           target: Path) -> None:
    """Resume a paused plan: optional guidance for the next BUILD, fresh attempts for the paused contracts, then run.

    Exit 4 refused, 1 when the intent commit fails; otherwise as `ateam run` (0 with --no-run)."""
    from datetime import UTC, datetime

    from ateam import scheduler
    from ateam.contracts.store import load_contracts
    from ateam.gitops import GitError
    from ateam.guidance import GuidanceEntry, append_guidance
    from ateam.plans.ops import plan_contract_ids, set_plan_status
    from ateam.plans.store import load_plans

    ctx = _context(target)
    plan = load_plans(ctx.paths.plans).get(plan_id)
    if plan is None:
        _fail(f"unknown plan {plan_id}", code=4)
    stored = ctx.db.feature_status(plan_id)
    if "paused" not in (stored, str(plan.status)):
        _fail(f"{plan_id} is {stored or plan.status}; only a paused plan resumes", code=4)
    require_no_live_run(ctx)
    require_no_live_planning(ctx)
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        _fail(str(e), code=4)
    pause = ctx.db.open_pause(plan_id)
    if pause is not None and pause["conflict"] and not pause["contracts"]:   # a contract's conflict resumes into its rebase BUILD
        _fail(f"{plan_id} paused on a merge conflict of the target branch ({pause['detail']}); resume cannot rebuild it", code=4)
    cids = plan_contract_ids(ctx.db, plan)
    problems = scheduler.binding_problems(ctx, cids)
    if problems:
        _fail(f"{plan_id} cannot resume until every contract binds:\n  " + "\n  ".join(problems), code=4)

    if adopt and (note is not None or note_file is not None):
        _fail("use --adopt or --note/--note-file, not both", code=4)
    if note is not None and not note.strip():
        _fail("the note is blank; write an instruction or leave out --note", code=4)
    if note_file is not None and not note_file.read_text().strip():
        _fail(f"the note file is blank ({note_file}); write an instruction or leave out --note-file", code=4)
    text = note_file.read_text().strip() if note_file is not None else note
    paused_recs = {c["contract_id"]: c for c in (pause or {}).get("contracts", [])}
    unfinished = [cid for cid in cids if ctx.db.contract_status(cid) != "integrated"]
    for cid in contract_ids:
        if cid not in cids:
            _fail(f"{cid} is not a contract of {plan_id}", code=4)
        if ctx.db.contract_status(cid) == "integrated":
            _fail(f"{cid} is integrated; an instruction has no BUILD to reach", code=4)
    targets = list(contract_ids) or (list(paused_recs) if adopt else (list(paused_recs) if len(paused_recs) == 1 else []))
    instructions: dict[str, tuple[str, str]] = {}   # cid -> (source, text)
    if adopt or text is not None:
        if not unfinished:
            _fail(f"every contract of {plan_id} is integrated; an instruction has no BUILD to reach (resume re-runs integration)", code=4)
        if adopt:
            if not targets:
                _fail(f"nothing to adopt: {plan_id}'s pause names no contract with a suggestion", code=4)
            for cid in targets:
                rec = paused_recs.get(cid)
                suggestion = ((rec or {}).get("findings") or {}).get("suggested_instruction")
                if not suggestion:
                    _fail(f"no suggestion for {cid} to adopt; write your own with --note", code=4)
                if rec["contract_hash"] != ctx.db.contract_hash(cid):
                    _fail(f"the suggestion for {cid} was written before its re-freeze; write your own with --note", code=4)
                instructions[cid] = (f"A-{rec['attempt_id']}" if rec.get("attempt_id") is not None else "A-?", suggestion)
        else:
            if not targets:
                _fail(f"say which contract the instruction is for: --contract C-xxxx (paused: {', '.join(paused_recs) or 'none'})", code=4)
            for cid in targets:
                instructions[cid] = ("owner", text)

    window_cids = sorted(paused_recs)
    if window_cids:
        ctx.db.resume_pause(None, window_cids)   # first, and idempotent: a repeated resume opens a newer window
    elif pause is None:
        click.echo("no pause record; no contract's attempts were refreshed")
    else:
        click.echo("no contract's attempts were refreshed (the pause names no contract)")
    cs = load_contracts(ctx.paths.contracts)
    now = datetime.now(UTC)
    written = [append_guidance(ctx.paths.guidance, cid, GuidanceEntry(at=now, source=src, contract_hash=cs[cid].content_hash(), text=t))
               for cid, (src, t) in instructions.items()]
    try:
        set_plan_status(ctx.paths, ctx.db, plan, ["executing"], by="owner", why="resumed", extra_files=written)
    except (IntentError, GitError) as e:
        git = GitRepo(ctx.paths.intent)
        click.echo(f"error: could not commit the resume in the intent repo: {e}")
        click.echo(f"{plan_id} is still paused. Restore the intent repo, then resume again:")
        for path in [*(ctx.paths.plans.glob(f"{plan_id}-*.yaml")), *written]:
            rel = intent_relpath(ctx.paths, path)
            tracked = bool(git.run("ls-tree", "--name-only", "HEAD", "--", rel).strip())
            verb = "checkout" if tracked else "clean -f"
            click.echo(f'  git -C "{ctx.paths.intent}" {verb} -- {rel}')
        sys.exit(1)
    if pause is not None:
        ctx.db.resume_pause(pause["pause_id"], [])
    for cid, (src, t) in instructions.items():
        click.echo(f"{cid}: instruction recorded ({'owner' if src == 'owner' else 'adopted from ' + src}): {t}")
    click.echo(f"{plan_id} -> executing (resumed)")
    if no_run:
        sys.exit(0)
    _run_foreground(target, plan_id, False)


@main.command("merge")
@click.argument("plan_id")
@TARGET_OPTION
def merge_cmd(plan_id: str, target: Path) -> None:
    """Fast-forward the target branch to the proposed integration tip, in your checkout. Nothing is pushed.

    Exit 0 merged, 1 fast-forward impossible (the target moved), 4 refused."""
    from ateam.gitops import GitError
    from ateam.merge import FastForwardImpossible, MergeRefused, merge_plan

    ctx = _context(target)
    require_no_live_run(ctx)
    try:
        res = merge_plan(ctx, plan_id)
    except FastForwardImpossible as e:
        _fail(str(e), code=1)
    except MergeRefused as e:
        _fail(str(e), code=4)
    except (GitError, IntentError) as e:
        _fail(f"git failed during the merge: {e}")
    if res.old_head == res.new_head:
        click.echo(f"{res.target_branch} already at {res.new_head[:12]}")
    else:
        click.echo(f"{res.target_branch} fast-forwarded to {res.new_head[:12]} (from {res.old_head[:12]})")
    click.echo(f"{plan_id} -> merged. Nothing was pushed.")


@main.group()
def plans() -> None:
    """Plans (feature requests and their contracts)."""


@plans.command("list")
@TARGET_OPTION
def plans_list(target: Path) -> None:
    from ateam.plans.store import load_plans

    ctx = _context(target)
    for pid, p in sorted(load_plans(ctx.paths.plans).items()):
        click.echo(f"{pid}  {p.status:<14} {len(p.contracts)} contract(s)  {p.title}")


@plans.command("show")
@click.argument("plan_id")
@TARGET_OPTION
def plans_show(plan_id: str, target: Path) -> None:
    import yaml

    from ateam.plans.store import load_plans

    ctx = _context(target)
    ps = load_plans(ctx.paths.plans)
    if plan_id not in ps:
        _fail(f"unknown plan {plan_id}")
    click.echo(yaml.safe_dump(ps[plan_id].model_dump(mode="json"), sort_keys=False))


# ---------------------------------------------------------------- records


@main.group()
def records() -> None:
    """Decision, invariant and convention records."""


@records.command("validate")
@TARGET_OPTION
def records_validate(target: Path) -> None:
    from ateam.records.store import RecordStoreError, load_records
    from ateam.records.validate import validate_records

    ctx = _context(target)
    try:
        recs = load_records(ctx.paths.records)
    except RecordStoreError as e:
        click.echo(f"ERROR -: {e}")
        sys.exit(1)
    problems = validate_records(recs, ctx.paths.root, ctx.db.record_hashes())
    for p in problems:
        tag = "ERROR" if p.level == "error" else "WARN"
        click.echo(f"{tag} {p.record_id or '-'}: {p.message}")
    errors = [p for p in problems if p.level == "error"]
    click.echo(f"{len(errors)} error(s), {len(problems) - len(errors)} warning(s)")
    sys.exit(1 if errors else 0)


@records.command("list")
@click.option("--status", "status", default=None, help="Only records with this status.")
@TARGET_OPTION
def records_list(status: str | None, target: Path) -> None:
    from ateam.records.store import load_records

    ctx = _context(target)
    for rid, r in sorted(load_records(ctx.paths.records).items()):
        if status and r.status != status:
            continue
        scope = ", ".join(r.binds.paths) if r.binds.paths else "(global)"
        click.echo(f"{rid}  {r.status:<10} {r.kind:<10} {r.statement}  [{scope}]")


@records.command("show")
@click.argument("record_id")
@TARGET_OPTION
def records_show(record_id: str, target: Path) -> None:
    import yaml

    from ateam.records.store import load_records

    ctx = _context(target)
    recs = load_records(ctx.paths.records)
    if record_id not in recs:
        _fail(f"unknown record {record_id}")
    click.echo(yaml.safe_dump(recs[record_id].model_dump(mode="json"), sort_keys=False))


def _service(ctx: Context):
    from ateam.records.service import RecordService

    return RecordService(ctx.paths, ctx.config)


def _print_overlaps(ctx: Context, record_id: str) -> None:
    from ateam.records.graph import RecordGraph
    from ateam.records.model import Status
    from ateam.records.store import load_records

    recs = load_records(ctx.paths.records)
    candidate = recs.get(record_id)
    if candidate is None:
        return
    graph = RecordGraph(recs)
    overlapping: dict[str, str] = {}
    for g in candidate.binds.paths:
        for other in graph.records.values():
            if other.id != record_id and other.status == Status.approved and any(
                _overlap(g, og) for og in other.binds.paths
            ):
                overlapping[other.id] = other.statement
    if overlapping:
        click.echo("Overlapping approved records:")
        for oid, statement in sorted(overlapping.items()):
            click.echo(f"  {oid}: {statement}")


def _overlap(a: str, b: str) -> bool:
    from ateam.contracts.select import _globs_overlap

    return _globs_overlap(a, b) or _globs_overlap(b, a)


def _lifecycle(ctx: Context, action, *records_out: str) -> None:
    from ateam.records.lifecycle import LifecycleError

    try:
        result = action()
    except LifecycleError as e:
        _fail(str(e))
    changed = result if isinstance(result, tuple) else (result,)
    for r in changed:
        ctx.db.set_record_hash(r.id, r.content_hash())
        click.echo(f"{r.id} -> {r.status}")


@records.command("review")
@click.option("--all", "review_all", is_flag=True, help="Review every record, not only proposed ones.")
@TARGET_OPTION
def records_review(review_all: bool, target: Path) -> None:
    """Walk through proposed records: approve as requirement or observation, reject with a reason, or skip."""
    from ateam.records.store import load_records

    ctx = _context(target)
    require_no_live_run(ctx)
    recs = load_records(ctx.paths.records)
    proposed = [rid for rid, r in sorted(recs.items()) if r.status == "proposed"]
    if review_all:
        for rid, r in sorted(recs.items()):
            if r.status != "proposed":
                click.echo(f"{rid}  {r.status:<10} {r.statement.strip()[:90]}")
        if not proposed:
            click.echo("Nothing to decide: no proposed records.")
            return
    elif not proposed:
        click.echo("Nothing proposed. Run ateam init or ateam plan to get proposals.")
        return
    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    _review_records(ctx, proposed, counts)
    click.echo(f"approved {counts['approved']}, rejected {counts['rejected']}, skipped {counts['skipped']}")


def _review_records(ctx: Context, record_ids: list[str], counts: dict[str, int]) -> None:
    import yaml

    from ateam.records.lifecycle import LifecycleError
    from ateam.records.model import Basis
    from ateam.records.store import load_records

    svc = _service(ctx)
    for rid in record_ids:
        recs = load_records(ctx.paths.records)
        if rid not in recs or recs[rid].status != "proposed":
            continue
        click.echo("\n" + yaml.safe_dump(recs[rid].model_dump(mode="json", exclude={"history", "fingerprint"}), sort_keys=False))
        _print_overlaps(ctx, rid)
        choice = click.prompt(f"{rid}: approve as [r]equirement, approve as [o]bservation, re[j]ect, [s]kip, [q]uit",
                              type=click.Choice(["r", "o", "j", "s", "q"]), show_choices=False)
        try:
            if choice in ("r", "o"):
                rec = svc.approve(rid, Basis.requirement if choice == "r" else Basis.observation)
                ctx.db.set_record_hash(rec.id, rec.content_hash()); counts["approved"] += 1
            elif choice == "j":
                reason = click.prompt("reason")
                rec = svc.reject(rid, reason)
                ctx.db.set_record_hash(rec.id, rec.content_hash()); counts["rejected"] += 1
            elif choice == "s":
                counts["skipped"] += 1
            else:
                counts["skipped"] += len(record_ids) - record_ids.index(rid)
                break
        except LifecycleError as e:
            click.echo(f"error: {e}")


@records.command("approve")
@click.argument("record_id")
@click.option("--basis", type=click.Choice(["requirement", "observation"]), default=None)
@TARGET_OPTION
def records_approve(record_id: str, basis: str | None, target: Path) -> None:
    from ateam.records.model import Basis

    ctx = _context(target)
    require_no_live_run(ctx)
    _print_overlaps(ctx, record_id)
    _lifecycle(ctx, lambda: _service(ctx).approve(record_id, Basis(basis) if basis else None))


@records.command("reject")
@click.argument("record_id")
@click.argument("reason")
@TARGET_OPTION
def records_reject(record_id: str, reason: str, target: Path) -> None:
    ctx = _context(target)
    require_no_live_run(ctx)
    _lifecycle(ctx, lambda: _service(ctx).reject(record_id, reason))


@records.command("supersede")
@click.argument("old_id")
@click.option("--with", "new_id", required=True, help="The proposed record that replaces it.")
@TARGET_OPTION
def records_supersede(old_id: str, new_id: str, target: Path) -> None:
    ctx = _context(target)
    require_no_live_run(ctx)
    _lifecycle(ctx, lambda: _service(ctx).supersede(old_id, new_id))


@records.command("retire")
@click.argument("record_id")
@click.argument("reason")
@TARGET_OPTION
def records_retire(record_id: str, reason: str, target: Path) -> None:
    ctx = _context(target)
    require_no_live_run(ctx)
    _lifecycle(ctx, lambda: _service(ctx).retire(record_id, reason))


# ---------------------------------------------------------------- contracts


@main.group()
def contracts() -> None:
    """Task contracts."""


@contracts.command("show")
@click.argument("contract_id")
@TARGET_OPTION
def contracts_show(contract_id: str, target: Path) -> None:
    import yaml

    from ateam.contracts.store import load_contracts

    ctx = _context(target)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    click.echo(yaml.safe_dump(cs[contract_id].model_dump(mode="json"), sort_keys=False))


@contracts.command("freeze")
@click.argument("contract_id")
@click.option("--force", is_flag=True, help="Re-freeze a contract that is no longer a draft.")
@TARGET_OPTION
def contracts_freeze(contract_id: str, force: bool, target: Path) -> None:
    from ateam.contracts.model import ContractStatus
    from ateam.contracts.select import freeze, select_records
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.plans.store import load_plans
    from ateam.records.graph import RecordGraph
    from ateam.records.store import load_records

    ctx = _context(target)
    require_no_live_run(ctx)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    if ContractStatus.superseded in (cs[contract_id].status, ctx.db.contract_status(contract_id)):
        _fail(f"{contract_id} is superseded; a superseded contract is never frozen again")
    owner = cs[contract_id].plan
    owner_file = load_plans(ctx.paths.plans).get(owner)
    for owner_status in (owner_file.status if owner_file else None, ctx.db.feature_status(owner)):
        if owner_status in ("plan_rejected", "plan_covered", "superseded"):
            _fail(f"{contract_id} belongs to {owner}, which is {owner_status}; its contracts are never frozen")
    if cs[contract_id].status != ContractStatus.draft and not force:
        _fail(f"{contract_id} is already frozen ({cs[contract_id].status}); pass --force to re-freeze and reset it")
    recs = load_records(ctx.paths.records)
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        _fail(str(e))
    graph = RecordGraph(recs)
    selected = select_records(cs[contract_id].paths, graph, ctx.paths.root)
    frozen = freeze(cs[contract_id], selected)
    saved = save_contract(ctx.paths.contracts, frozen)
    try:
        commit_intent(ctx.paths, f"ateam: freeze {frozen.id}", [f"contracts/{saved.name}"])
    except IntentError as e:
        if "nothing to commit" not in str(e):
            _fail(f"{e}. The frozen file is saved but not committed; commit it in {ctx.paths.intent} or re-run with --force")
    ctx.db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
    for r in selected:
        ctx.db.set_record_hash(r.id, r.content_hash())
    click.echo(f"{frozen.id} frozen as {frozen.status} with records: {', '.join(r.id for r in selected) or '(none)'}")


@contracts.command("refresh")
@click.argument("plan_id", required=False)
@RESULT_FILE_OPTION
@TARGET_OPTION
def contracts_refresh(plan_id: str | None, result_file: Path | None, target: Path) -> None:
    """Promote frozen draft contracts whose dependencies are all verified or integrated. Stops on drift: a drifted
    draft is left as it is and reported (owner, 2026-10-25). Only the status changes; the frozen hash does not."""
    from ateam.contracts.model import ContractStatus
    from ateam.contracts.select import drift
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.plans.store import load_plans
    from ateam.records.store import load_records

    with _Result(result_file, {"promoted": [], "drifted": []}) as out:
        ctx = _context(target)
        plans_ = load_plans(ctx.paths.plans)
        if plan_id is not None and plan_id not in plans_:
            _fail(f"unknown plan {plan_id}")
        cs = load_contracts(ctx.paths.contracts)
        recs = load_records(ctx.paths.records)
        live = ("plan_approved", "executing", "integrating")
        promote, problems = [], 0
        for cid, c in sorted(cs.items()):
            if plan_id is not None and c.plan != plan_id:
                continue
            frozen = ctx.db.contract_hash(cid)
            if c.status != ContractStatus.draft or frozen is None or ctx.db.contract_status(cid) != "draft":
                continue
            if ctx.db.feature_status(c.plan) not in live:
                continue
            waiting = [d for d in c.depends_on if ctx.db.contract_status(d) not in ("verified", "integrated")]
            if waiting:
                click.echo(f"{cid}: waiting on " + ", ".join(f"{d} ({ctx.db.contract_status(d) or 'missing'})" for d in waiting))
                continue
            if c.content_hash() != frozen:
                click.echo(f"{cid}: differs from its frozen hash (hand edit); left draft")
                out["drifted"].append(cid)
                problems += 1
                continue
            drifted = drift(c, recs)
            if drifted:
                click.echo(f"{cid}: record drift ({', '.join(drifted)}); left draft")
                out["drifted"].append(cid)
                problems += 1
                continue
            promote.append(c)
        if not promote:
            click.echo("nothing to refresh")
            sys.exit(1 if problems else 0)
        try:
            require_clean(ctx.paths)
        except IntentError as e:
            _fail(str(e))
        files = [save_contract(ctx.paths.contracts, c.model_copy(update={"status": ContractStatus.ready})) for c in promote]
        commit_intent_files(ctx.paths, f"ateam: refresh {', '.join(c.id for c in promote)}", files)
        for c in promote:
            ctx.db.transition("contract", c.id, "ready", by="refresh")
            click.echo(f"{c.id} -> ready")
            out["promoted"].append(c.id)
        sys.exit(1 if problems else 0)


@contracts.command("validate")
@TARGET_OPTION
def contracts_validate(target: Path) -> None:
    """Check frozen contracts against the state store: hand edits and record drift."""
    from ateam.contracts.select import drift
    from ateam.contracts.store import ContractStoreError, load_contracts
    from ateam.records.store import RecordStoreError, load_records

    ctx = _context(target)
    try:
        cs = load_contracts(ctx.paths.contracts)
        recs = load_records(ctx.paths.records)
    except (ContractStoreError, RecordStoreError) as e:
        click.echo(f"ERROR -: {e}")
        sys.exit(1)
    errors = 0
    for cid, c in sorted(cs.items()):
        frozen = ctx.db.contract_hash(cid)
        if frozen is None:
            click.echo(f"INFO {cid}: not frozen ({c.status})")
            continue
        if ctx.db.contract_status(cid) == "superseded":
            click.echo(f"INFO {cid}: superseded")
            continue
        if c.content_hash() != frozen:
            click.echo(f"ERROR {cid}: hand edit; content differs from the hash stored at freeze")
            errors += 1
        drifted = drift(c, recs)
        if drifted:
            click.echo(f"ERROR {cid}: record drift; {', '.join(drifted)} changed, superseded or missing since freeze")
            errors += 1
    click.echo(f"{errors} error(s)")
    sys.exit(1 if errors else 0)


@main.command()
@click.argument("kind", type=click.Choice(["records", "contracts", "config"]))
def schema(kind: str) -> None:
    """Print the JSON Schema for records, contracts or config (for editors and validation)."""
    import json

    from ateam.config import TargetConfig
    from ateam.contracts.model import Contract
    from ateam.records.model import Record

    model = {"records": Record, "contracts": Contract, "config": TargetConfig}[kind]
    click.echo(json.dumps(model.model_json_schema(), indent=2))


# ---------------------------------------------------------------- status, doctor


@main.command()
@click.argument("plan_id", required=False)
@TARGET_OPTION
def status(plan_id: str | None, target: Path) -> None:
    """Feature and contract state from the operational store; with a plan id, that plan in detail, including why it
    paused, what the investigation found and how to resume it."""
    from ateam.plans.store import load_plans

    ctx = _context(target)
    plans_ = load_plans(ctx.paths.plans)
    if plan_id is not None:
        if plan_id not in plans_:
            _fail(f"unknown plan {plan_id}", code=4)
        _status_plan(ctx, plans_[plan_id])
        return
    click.echo("Plans:" if plans_ else "Plans: none")
    for pid, p in sorted(plans_.items()):
        live = ctx.db.feature_status(pid) or p.status
        click.echo(f"  {pid}  {live:<14} {len(p.contracts)} contract(s)  {p.title}")
    crow = ctx.db.conn.execute("SELECT contract_id, plan_id, status FROM contracts ORDER BY contract_id").fetchall()
    click.echo("Contracts:" if crow else "Contracts: none")
    for r in crow:
        last = ctx.db.latest_attempt(r["contract_id"])
        attempt = f"  last attempt: {last['role']} {last['outcome'] or 'running'}" if last else ""
        click.echo(f"  {r['contract_id']}  {r['plan_id']}  {r['status']}{attempt}")
    holder = ctx.db.run_lock_holder()
    if holder:
        click.echo(f"Run lock held by {holder}")
    for pid in sorted(plans_):
        if (ctx.db.feature_status(pid) or str(plans_[pid].status)) == "paused":
            pause = ctx.db.open_pause(pid)
            reason = pause["reason"] if pause else "no reason recorded"
            click.echo(f"  {pid} paused ({reason}): ateam status {pid}")


def _echo_lanes(ctx: Context, plan) -> None:
    """The plan's lanes (design 2026-10-27): contracts that run one after another in one worktree."""
    from ateam.contracts.store import load_contracts
    from ateam.plans.ops import plan_contract_ids
    from ateam.scheduler import lane_lines, plan_lanes

    cids = plan_contract_ids(ctx.db, plan)
    cs = load_contracts(ctx.paths.contracts)
    if cids and all(c in cs for c in cids):
        for line in lane_lines(plan_lanes(ctx, plan.id, cs, cids), ctx.config.max_workers):
            click.echo(line)


def _status_plan(ctx: Context, plan) -> None:
    from ateam.plans.ops import plan_contract_ids
    from ateam.worktrees import worktree_path

    pid = plan.id
    live = ctx.db.feature_status(pid) or str(plan.status)
    click.echo(f"{pid}  {live}  {plan.title}")
    for cid in plan_contract_ids(ctx.db, plan):
        last = ctx.db.latest_attempt(cid)
        click.echo(f"  {cid}  {ctx.db.contract_status(cid)}" + (f"  last attempt: {last['role']} {last['outcome'] or 'running'}" if last else ""))
    _echo_lanes(ctx, plan)
    if live != "paused":
        return
    pause = ctx.db.open_pause(pid)
    if pause is None:
        last = (ctx.db.transitions("feature", pid) or [{}])[-1].get("reason") or "no reason recorded"
        click.echo(f"\npaused ({last}); no pause record.\nResume: ateam resume {pid}")
        return
    click.echo(f"\npaused ({pause['reason']}): {pause['detail']}")
    for c in pause["contracts"]:
        click.echo(f"\n{c['contract_id']} ({c['reason']}): {c['detail']}")
        click.echo(f"  worktree: {worktree_path(ctx.paths, ctx.db, c['contract_id'])}")
        f = c["findings"]
        if not f:
            click.echo(f"  no findings: {c['note'] or 'no investigation ran'}")
            continue
        click.echo(f"  findings: {f.get('summary')}")
        if f.get("failure_class"):
            click.echo(f"  failure class: {f['failure_class']}")
        for e in f.get("evidence") or []:
            click.echo(f"  - {e.get('ref')}: {e.get('shows')}")
        if f.get("needs_owner_decision"):
            click.echo(f"  needs your decision: {f['needs_owner_decision']}")
        if f.get("suggested_instruction"):
            click.echo(f"  suggested instruction: {f['suggested_instruction']}")
    if pause["conflict"] and not pause["contracts"]:
        click.echo("\nthis is a merge conflict of the target branch; resume cannot rebuild it (reject the plan and plan again)")
        return
    if pause["conflict"]:
        click.echo(f"\nresume runs the rebase BUILD for {', '.join(c['contract_id'] for c in pause['contracts'])}")
    contracts = pause["contracts"]
    adoptable = bool(contracts) and all(
        (c["findings"] or {}).get("suggested_instruction") and c["contract_hash"] == ctx.db.contract_hash(c["contract_id"])
        for c in contracts)
    click.echo("\nTo continue:")
    if adoptable:
        click.echo(f"  ateam resume {pid} --adopt                    # use the suggestion as your instruction")
    if len(contracts) > 1:
        for c in contracts:
            click.echo(f'  ateam resume {pid} --note "use the helper in src/util/x.py" --contract {c["contract_id"]}')
    else:
        click.echo(f'  ateam resume {pid} --note "use the helper in src/util/x.py"')
    click.echo(f"  ateam resume {pid}                            # resume with no instruction")


@main.command()
@TARGET_OPTION
def doctor(target: Path) -> None:
    """Versions, target branch and workspace health."""
    import claude_agent_sdk
    from claude_agent_sdk._cli_version import __cli_version__

    click.echo(f"ateam {__version__}")
    click.echo(f"SDK {claude_agent_sdk.__version__}, bundled Claude Code {__cli_version__}, spiked against {SPIKED_CLI_VERSION}")
    if __cli_version__ != SPIKED_CLI_VERSION:
        click.echo("WARN bundled Claude Code differs from the spiked version; re-run the spike before trusting the clean room")
    paths = TargetPaths.from_root(target)
    click.echo(f"repo id {repo_id(target)} (from {_derivation(target)})")
    moved = _previous_home(target)
    if moved:
        previous, old_home = moved
        click.echo(f"WARN repo id derivation changed: now from {_derivation(target)} ({repo_id(target)}), was {previous['source']} "
                   f"({previous['id']}). The intent repo for the old id is still at {old_home / 'intent'}; nothing was moved. "
                   f"Run: ateam intent init --adopt  to move it, or --fresh to start over")
    else:
        record_target(target)
    click.echo(f"intent repo {paths.intent}: {'present' if paths.exists() else 'missing (run: ateam intent init)'}")
    if paths.exists():
        remotes = GitRepo(paths.intent).run("remote", "-v").strip().splitlines()
        click.echo("intent remote " + (remotes[0].split()[1] if remotes else "none"))
    try:
        git = GitRepo.discover(target)
        cfg = load_config(paths)
        click.echo(f"target branch {git.current_branch()} (configured target branch {cfg.target_branch})")
    except Exception as e:  # noqa: BLE001
        click.echo(f"git: {e}")


# ---------------------------------------------------------------- brief, worker


@main.command()
@click.argument("contract_id")
@click.option("--role", type=click.Choice(["build", "investigate", "test"]), required=True)
@TARGET_OPTION
def brief(contract_id: str, role: str, target: Path) -> None:
    """Print the brief a worker would receive, without running anything."""
    from ateam.brief import assemble_brief, render_brief
    from ateam.checkpoint import Checkpoint
    from ateam.contracts.store import load_contracts
    from ateam.guidance import live_guidance, load_guidance
    from ateam.records.store import load_records
    from ateam.roles import Role

    ctx = _context(target)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    recs = load_records(ctx.paths.records)
    selected = [recs[ref.id] for ref in cs[contract_id].records if ref.id in recs]
    prev_raw = ctx.db.latest_checkpoint(contract_id, role="build") if Role(role) is Role.build else ctx.db.latest_checkpoint(contract_id)
    prev = Checkpoint.from_dict(prev_raw) if prev_raw else None
    scratch = ctx.paths.scratch_root / (f"{contract_id}-test" if Role(role) is Role.test else contract_id)
    instructions = (live_guidance(load_guidance(ctx.paths.guidance, contract_id), cs[contract_id].content_hash())
                    if Role(role) is Role.build else [])
    click.echo(render_brief(assemble_brief(cs[contract_id], selected, prev, Role(role), scratch_dir=scratch,
                                           instructions=instructions)))


def _require_isolated_worktree(ctx: Context, worktree: Path) -> GitRepo:
    """The linked worktree of the target at `worktree`, or exit 4: never the owner's checkout, a nested or a foreign one."""
    from ateam.gitops import GitError

    try:
        wt_git = GitRepo.discover(worktree)
        linked = wt_git.is_linked_worktree()
    except GitError:
        linked = False
    target_root = ctx.paths.root.resolve()
    if not linked or wt_git.root.resolve() == target_root:
        _fail(f"{worktree} is not an isolated worktree of the target; workers never run in the owner's checkout", code=4)
    if target_root in wt_git.root.resolve().parents:
        _fail(f"{worktree} is inside the target checkout; create worktrees outside it", code=4)
    wt_common = (wt_git.root / wt_git.run("rev-parse", "--git-common-dir").strip()).resolve()
    target_git = (ctx.paths.root / ctx.git.run("rev-parse", "--git-dir").strip()).resolve()
    if wt_common != target_git:
        _fail(f"{worktree} is not a worktree of the target repository {ctx.paths.root}", code=4)
    return wt_git


@main.group()
def worker() -> None:
    """Run a single worker (low-level; the scheduler arrives in pass two)."""


@worker.command("run")
@click.argument("contract_id")
@click.option("--role", type=click.Choice(["build", "investigate", "test"]), required=True)
@click.option("--cwd", "worktree", type=click.Path(file_okay=False, exists=True, path_type=Path), required=True, help="Worktree to run in.")
@click.option("--base", default=None, help="Base commit for the diff and scope check (default: merge-base with the target branch).")
@click.option("--tier", default=None, help="Override the contract's tier for this role.")
@click.option("--resume", default=None, help="Resume a previous session id.")
@click.option("--prompt", default=None, help="Override the default prompt.")
@RESULT_FILE_OPTION
@TARGET_OPTION
def worker_run(contract_id: str, role: str, worktree: Path, base: str | None, tier: str | None, resume: str | None, prompt: str | None,
               result_file: Path | None, target: Path) -> None:
    import asyncio
    import json

    from ateam.contracts.select import drift
    from ateam.contracts.store import load_contracts
    from ateam.records.store import load_records
    from ateam.roles import Role
    from ateam.runner.run import SetupError, default_base, ensure_setup, run_contract_attempt
    from ateam.tiers import load_tiers

    from ateam.runner.binding import ContractNotBinding, require_binding

    with _Result(result_file, {"outcome": "refused", "subtype": None, "error": None, "cost_usd": 0.0, "session_id": None,
                               "test_outcome": None, "test_class": None, "report": None, "refusal": None}) as out:
        ctx = _context(target)
        cs = load_contracts(ctx.paths.contracts)
        if contract_id not in cs:
            _fail(f"unknown contract {contract_id}")
        contract = cs[contract_id]
        records = load_records(ctx.paths.records)
        try:
            require_binding(contract, records, ctx.db)   # before setup: a contract that does not bind never provisions a worktree
        except ContractNotBinding as e:
            out["outcome"] = "not_binding"
            out["refusal"] = "records_drifted" if drift(contract, records) else "not_binding"
            _fail(str(e), code=4)
        wt_git = _require_isolated_worktree(ctx, worktree)
        worktree = wt_git.root
        tiers = load_tiers(ctx.config.tiers)
        if tier and tier not in tiers:
            _fail(f"unknown tier {tier!r}; known: {', '.join(tiers)}")
        base = base or default_base(wt_git, ctx.config.target_branch)
        try:
            ensure_setup(worktree, ctx.config, ctx.db, base, on_command=lambda c: click.echo(f"setup: {c}"))
        except SetupError as e:
            out["outcome"] = "setup_failed"
            _fail(f"setup failed: {e.cmd} (exit {e.returncode})\n{e.output}")
        try:
            res, tree, scope, cp = asyncio.run(run_contract_attempt(
                paths=ctx.paths, config=ctx.config, git_worktree=wt_git, worktree=worktree,
                base=base,
                contract=contract, records=records, role=Role(role), tiers=tiers,
                db=ctx.db, resume=resume, prompt=prompt, tier_override=tier,
            ))
        except ContractNotBinding as e:
            out["outcome"] = "not_binding"
            out["refusal"] = "records_drifted" if drift(contract, records) else "not_binding"
            _fail(str(e), code=4)
        except ValueError as e:
            _fail(str(e))
        out.update(subtype=res.subtype, error=res.error, cost_usd=res.cost_usd, session_id=res.session_id, report=res.report)
        click.echo(f"session {res.session_id} subtype={res.subtype} turns={res.num_turns} cost=${res.cost_usd:.4f} wall={res.wall_s:.0f}s")
        if res.denials:
            click.echo("denials: " + "; ".join(res.denials))
        if res.error:
            click.echo(f"error: {res.error}")
        click.echo(f"scope ok={scope.ok} outside={scope.outside} dirty={scope.dirty} changed={tree.changed_vs_base}")
        code = 0 if (res.report and scope.ok) else (3 if res.report else 1)
        out["outcome"] = {0: "completed", 3: "scope_breach", 1: "failed"}[code]
        last = ctx.db.latest_attempt(contract_id)
        if last and last.get("outcome") == "tamper":
            out["outcome"] = "tamper"   # the intent repo changed during the session (layer 4)
            click.echo("tamper: the intent repo changed during the session")
        if Role(role) is Role.test and res.report and scope.ok:
            from ateam.runner.run import classify_test_report

            test_outcome, test_class = classify_test_report(res.report)
            out.update(test_outcome=test_outcome, test_class=test_class)
            click.echo(f"classification: {test_outcome}/{test_class or '-'}")
            if test_outcome == "failed":
                code = 5   # a failed verdict (owner, 2026-10-26); 0 met, 3 scope breach, 1 no report, 4 refused
        click.echo(json.dumps(res.report, indent=2) if res.report else "no structured report")
        sys.exit(code)


@worker.command("pause-investigate")
@click.argument("plan_id")
@click.argument("contract_id")
@click.option("--cwd", "worktree", type=click.Path(file_okay=False, exists=True, path_type=Path), required=True,
              help="The paused contract's worktree.")
@click.option("--tier", default="careful", show_default=True, help="Tier for the INVESTIGATE session.")
@RESULT_FILE_OPTION
@TARGET_OPTION
def worker_pause_investigate(plan_id: str, contract_id: str, worktree: Path, tier: str, result_file: Path | None, target: Path) -> None:
    """Investigate why a contract paused: read-only, in its worktree; the findings go on the pause record for the owner.

    Exit 0 completed, 1 tamper or failed, 4 refused. `ateam run` starts this itself after a pause."""
    import asyncio

    from ateam.brief import assemble_pause_brief
    from ateam.checkpoint import Checkpoint
    from ateam.contracts.store import load_contracts
    from ateam.guidance import load_guidance
    from ateam.records.store import load_records
    from ateam.roles import PAUSE_INVESTIGATE_SPEC, Role
    from ateam.runner import investigate as inv
    from ateam.tiers import load_tiers

    with _Result(result_file, {"outcome": "refused", "attempt_id": None, "cost_usd": 0.0, "error": None}) as out:
        ctx = _context(target)
        cs = load_contracts(ctx.paths.contracts)
        if contract_id not in cs:
            _fail(f"unknown contract {contract_id}", code=4)
        pause = ctx.db.open_pause(plan_id)
        paused = next((c for c in (pause or {}).get("contracts", []) if c["contract_id"] == contract_id), None)
        if paused is None:
            _fail(f"{plan_id} has no open pause naming {contract_id}; nothing to investigate", code=4)
        wt_git = _require_isolated_worktree(ctx, worktree)
        tiers = load_tiers(ctx.config.tiers)
        if tier not in tiers:
            _fail(f"unknown tier {tier!r}; known: {', '.join(tiers)}", code=4)
        contract = cs[contract_id]
        recs = load_records(ctx.paths.records)
        prev = ctx.db.latest_checkpoint(contract_id, role="build")
        brief_text = assemble_pause_brief(
            contract, [recs[ref.id] for ref in contract.records if ref.id in recs], paused, ctx.db.attempts_for(contract_id),
            Checkpoint.from_dict(prev) if prev else None, load_guidance(ctx.paths.guidance, contract_id),
            runs_dir=home_dir(target) / "runs")
        result = asyncio.run(inv.run_investigation(
            paths=ctx.paths, config=ctx.config, target_git=wt_git, name=contract_id, scratch_name=f"{contract_id}-investigate",
            spec=PAUSE_INVESTIGATE_SPEC, brief_text=brief_text, tier=tiers[tier][Role.investigate], db=ctx.db, tier_name=tier,
            prompt=(f"Contract {contract_id} paused ({paused['reason']}). Find out why, with evidence, and end with the "
                    "structured report."),
        ))
        report = result.res.report
        note = f"tamper: {result.tampered[0]}" if result.tampered else (None if report else "no report")
        ctx.db.set_pause_findings(pause["pause_id"], contract_id, result.attempt_id, report if not result.tampered else None, note)
        outcome = "tamper" if result.tampered else ("completed" if report else "failed")
        out.update(outcome=outcome, attempt_id=result.attempt_id, cost_usd=result.res.cost_usd, error=result.res.error)
        click.echo(f"{contract_id} pause investigation: {outcome}" + (f" ({note})" if note else ""))
        if report and not result.tampered:
            click.echo(f"summary: {report.get('summary')}")
            if report.get("suggested_instruction"):
                click.echo(f"suggested instruction: {report['suggested_instruction']}")
        sys.exit(0 if outcome == "completed" else 1)


# ---------------------------------------------------------------- verify (minimal)


@main.command()
@click.argument("contract_id")
@click.option("--cwd", "worktree", type=click.Path(file_okay=False, exists=True, path_type=Path), required=True, help="Worktree holding the committed work.")
@click.option("--base", "base_rev", default=None, help="Base commit for the diff and scope check (default: merge-base with the target branch).")
@RESULT_FILE_OPTION
@TARGET_OPTION
def verify(contract_id: str, worktree: Path, base_rev: str | None, result_file: Path | None, target: Path) -> None:
    """Minimal verification: the contract's checks, scope and acceptance tests, judged from the worktree.

    No TEST worker yet: judgment criteria are listed, not checked. Exit 0 verified, 1 failed or refused, 4 guard."""
    import json
    import os

    from ateam import verify as v
    from ateam.checkpoint import Checkpoint
    from ateam.contracts.model import ContractStatus
    from ateam.contracts.select import drift
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.gitops import GitError
    from ateam.intent import intent_git
    from ateam.records.store import load_records
    from ateam.runner.binding import ContractNotBinding, require_binding
    from ateam.runner.run import default_base
    from ateam.treecheck import scope_check, tree_state

    with _Result(result_file, {"status": "refused", "failure_class": None, "reasons": [], "refusal": None}) as out:
        ctx = _context(target)
        cs = load_contracts(ctx.paths.contracts)
        if contract_id not in cs:
            _fail(f"unknown contract {contract_id}")
        wt_git = _require_isolated_worktree(ctx, worktree)
        contract = cs[contract_id]
        intent = intent_git(ctx.paths)

        def set_file_status(c, status: str, subject: str) -> None:
            saved = save_contract(ctx.paths.contracts, c.model_copy(update={"status": ContractStatus(status)}))
            try:
                commit_intent(ctx.paths, subject, [f"contracts/{saved.name}"])
            except (IntentError, GitError) as e:
                if "nothing to commit" not in str(e):
                    _fail(f"could not commit {contract_id} -> {status} in the intent repo: {e}; restore it with "
                          f"git -C \"{ctx.paths.intent}\" checkout -- contracts/{saved.name} and re-run")

        # Markers in the store: `running` before the checks start, `pending` (the verdict and its checkpoint) after them
        # and before the intent commit. A check runs as the owner and could forge either, so a marker can only ever
        # finish a failed verdict; a verified one is earned again by a run that completes its own checks.
        pending_key, running_key = f"verify-pending:{contract_id}", f"verify-running:{contract_id}"
        pending_raw, running_raw = ctx.db.get_meta(pending_key), ctx.db.get_meta(running_key)
        if pending_raw or running_raw:
            dirty = intent.status_porcelain()
            if dirty:
                _fail(f"an earlier verify of {contract_id} was interrupted and the intent repo has uncommitted changes "
                      f"({', '.join(path for _, path in dirty)}); review them, commit or discard them, and re-run", code=4)
            marker = json.loads(pending_raw or running_raw)
            # The walk starts from the store, never from the marker: a marker can be forged by a check.
            before_status = ctx.db.contract_status(contract_id)
            if before_status not in ("ready", "building", "failed"):
                _fail(f"an earlier verify of {contract_id} was interrupted and the state store says {before_status}; "
                      "investigate before verifying again", code=4)
            if pending_raw and marker["status"] == ContractStatus.failed:
                set_file_status(contract, "failed", f"ateam: verify {contract_id} -> failed")
                ctx.db.finish_verification(contract_id, before_status, v.walk_to(before_status, "failed"), by="verify",
                                           reason=str(marker.get("reason", ""))[:500], checkpoint=marker["checkpoint"],
                                           clear_keys=[pending_key, running_key])
                click.echo(f"recovered: an interrupted verify had judged {contract_id} failed; the state store now agrees")
                out.update(status="failed", failure_class=marker["checkpoint"].get("failure_class") or "implementation",
                           reasons=[str(marker.get("reason", ""))])
                sys.exit(1)
            if not pending_raw:
                note = "a previous verify was stopped while its checks ran"
                set_file_status(contract, "failed", f"ateam: verify {contract_id} -> failed")
                ctx.db.finish_verification(
                    contract_id, before_status, v.walk_to(before_status, "failed"), by="verify",
                    reason=f"{v.VERIFIER}; tamper: {note}", clear_keys=[pending_key, running_key],
                    checkpoint=Checkpoint(
                        base="", head="", commits=[], diff_names=[], dirty=[], checks=[], contract_hash=contract.content_hash(),
                        record_hashes={ref.id: ref.hash for ref in contract.records}, session_id=None, notes=None,
                        warnings=[v.VERIFIER, note],
                    ).to_dict(),
                )
                click.echo(f"  tamper: {note}")
                click.echo(f"{contract_id} -> failed (tamper)")
                out.update(status="failed", failure_class="tamper", reasons=[note])
                sys.exit(1)
            set_file_status(contract, before_status, f"ateam: verify {contract_id} interrupted; back to {before_status}")
            ctx.db.delete_meta(pending_key, running_key)
            contract = load_contracts(ctx.paths.contracts)[contract_id]
            click.echo(f"note: an earlier verify of {contract_id} was interrupted before it was recorded; verifying again")

        if contract.status == ContractStatus.paused:
            _fail(f"{contract_id} is paused; the owner resumes it before it is verified", code=4)
        records = load_records(ctx.paths.records)
        try:
            require_binding(contract, records, ctx.db)
            require_clean(ctx.paths)
        except (ContractNotBinding, IntentError) as e:
            if isinstance(e, ContractNotBinding):
                out["refusal"] = "records_drifted" if drift(contract, records) else "not_binding"
            else:
                out["refusal"] = "intent_dirty"
            _fail(str(e), code=4)
        if not contract.checks:
            _fail(f"minimal verify needs at least one deterministic check; {contract_id} has none")
        base = base_rev or default_base(wt_git, ctx.config.target_branch)
        tree = tree_state(wt_git, base)
        scope = scope_check(tree, contract.paths, contract.incidental)
        if scope.dirty:
            _fail(f"the worktree has uncommitted changes ({', '.join(scope.dirty)}); commit the work first")
        start = ctx.db.contract_status(contract_id)   # equal to the file's status: require_binding checked it

        click.echo(f"verifying {contract_id} in {wt_git.root} against base {base[:12]} ({v.VERIFIER})")
        ctx.db.set_meta(running_key, json.dumps({"start": start, "parent": intent.head()}))
        before = v.snapshot(wt_git, intent, ctx.git, ctx.db.fingerprint(contract_id))
        results = v.run_checks(contract.checks, wt_git.root, v.check_env(os.environ, ctx.config.env))
        after = v.snapshot(wt_git, intent, ctx.git, ctx.db.fingerprint(contract_id))
        tamper, warnings = v.compare(before, after)
        acceptance = v.acceptance_presence(contract, wt_git.root)
        outcome = v.decide(scope, results, acceptance, tamper)

        for r in results:
            state = "passed" if r.passed else ("errored" if r.errored else "failed")
            click.echo(f"  {r.name}: {state}" + ("" if r.passed or r.exit_code is None else f" (exit {r.exit_code})"))
            if not r.passed and r.tail.strip():
                click.echo("\n".join("      " + line for line in r.tail.splitlines()[-20:]))
        for a in acceptance:
            state = {True: "present", False: "MISSING", None: "not checked by minimal verify"}[a.ok]
            click.echo(f"  {a.id} [{a.kind}]: {state}" + ("" if a.ok is None else f" ({a.detail})"))
        if not scope.ok:
            click.echo("  scope: outside the contract's paths: " + ", ".join(scope.outside))
        for t in tamper:
            click.echo(f"  tamper: {t}")
        for w in warnings:
            click.echo(f"  warning: {w}")

        # Recorded from the contract as it was before the checks: nothing read after them is trusted.
        final = contract.model_copy(update={"status": ContractStatus(outcome.status)})
        steps = v.walk_to(start, outcome.status)
        log = wt_git.run("log", "--format=%h %s", f"{base}..{after.worktree_head}") if after.worktree_head != base else ""
        checkpoint = Checkpoint(
            base=base, head=after.worktree_head, commits=[line for line in log.splitlines() if line],
            diff_names=tree.changed_vs_base, dirty=[*after.worktree_tracked, *after.worktree_untracked],
            checks=[r.fact() for r in results], contract_hash=final.content_hash(),
            record_hashes={ref.id: ref.hash for ref in final.records}, session_id=None, notes=None,
            warnings=[v.VERIFIER, *warnings],
        ).to_dict()
        summary = (f"{outcome.failure_class}: " if outcome.failure_class else "") + ("; ".join(outcome.reasons) or "all checks passed")
        reason = f"{v.VERIFIER}; {summary}"[:500]
        subject = f"ateam: verify {contract_id} -> {outcome.status}"
        ctx.db.set_meta(pending_key, json.dumps({
            "status": outcome.status, "parent": intent.head(), "subject": subject, "start": start, "steps": steps,
            "reason": reason, "checkpoint": checkpoint,
        }, default=str))

        # The intent repo is the truth; the state store follows it.
        saved = save_contract(ctx.paths.contracts, final)
        rel = f"contracts/{saved.name}"
        try:
            commit_intent(ctx.paths, subject, [rel])
        except (IntentError, GitError) as e:
            if "nothing to commit" not in str(e):
                ctx.db.delete_meta(pending_key, running_key)
                click.echo(f"error: could not commit the verification in the intent repo: {e}")
                click.echo("Nothing was recorded in the state store. Restore the contract file and re-run:")
                click.echo(f"  git -C \"{ctx.paths.intent}\" checkout -- {rel}")
                sys.exit(1)
        ctx.db.finish_verification(contract_id, start, steps, by="verify", reason=reason, checkpoint=checkpoint,
                                   clear_keys=[pending_key, running_key])
        click.echo(f"{contract_id} -> {outcome.status}" + (f" ({outcome.failure_class})" if outcome.failure_class else ""))
        out.update(status=str(outcome.status), failure_class=outcome.failure_class, reasons=list(outcome.reasons))
        sys.exit(0 if outcome.status == ContractStatus.verified else 1)


if __name__ == "__main__":
    main()
