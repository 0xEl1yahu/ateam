"""The ateam command-line interface."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ateam import SPIKED_CLI_VERSION, __version__
from ateam.config import TargetConfig, load_config
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir, lookup_target, normalise_remote, origin_url, record_target, repo_id, repo_id_source
from ateam.runner.investigate import run_investigation
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
    return Context(target)


def _fail(message: str, code: int = 1) -> None:
    click.echo(f"error: {message}")
    sys.exit(code)


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

    try:
        click.echo(pull(TargetPaths.from_root(target)).strip() or "pulled")
    except (IntentError, GitError) as e:
        _fail(str(e))


def _derivation(target: Path) -> str:
    url = origin_url(target)
    return f"origin {normalise_remote(url)}" if url else "path"


# ---------------------------------------------------------------- init (discovery)


def _records_index(records) -> str:
    approved = [r for r in records.values() if r.status == "approved"]
    if not approved:
        return "Approved records: none yet."
    lines = ["Approved records (id, kind, binds, statement):"]
    lines += [f"- {r.id} {r.kind} [{', '.join(r.binds.paths) or 'global'}] {r.statement.strip()}" for r in sorted(approved, key=lambda r: r.id)]
    return "\n".join(lines)


@main.command()
@click.option("--remote", default=None, help="Private remote for the intent repo, when creating it.")
@click.option("--max-candidates", default=12, show_default=True, type=int)
@click.option("--tier", default="standard", show_default=True, type=click.Choice(["light", "standard", "careful"]))
@TARGET_OPTION
def init(remote: str | None, max_candidates: int, tier: str, target: Path) -> None:
    """Discover a small baseline of records from the target and propose them for review."""
    import asyncio

    from ateam.discovery import candidates_to_records, render_discovery_report
    from ateam.intent import IntentError, commit_intent, init_intent
    from ateam.records.store import load_records, save_record
    from ateam.roles import DISCOVERY_SPEC, Role
    from ateam.tiers import load_tiers

    paths = TargetPaths.from_root(target)
    if not paths.exists():
        try:
            init_intent(paths, remote=remote)
        except IntentError as e:
            _fail(str(e))
        record_target(target)
    ctx = Context(target)
    records = load_records(ctx.paths.records)
    brief = "\n\n".join([
        f"Propose at most {max_candidates} candidate records; at most {ctx.config.global_record_cap} may be global (no paths).",
        _records_index(records),
        "Do not re-propose anything already covered above.",
    ])
    tiers = load_tiers(ctx.config.tiers)
    result = asyncio.run(run_investigation(
        paths=ctx.paths, config=ctx.config, target_git=ctx.git, name="init", spec=DISCOVERY_SPEC,
        prompt="Discover this repository's existing engineering intent and report candidates.", brief_text=brief,
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


@main.command()
@click.argument("request")
@click.option("--tier", default="standard", show_default=True, type=click.Choice(["light", "standard", "careful"]))
@TARGET_OPTION
def plan(request: str, tier: str, target: Path) -> None:
    """Turn a feature request into contracts and a plan for approval."""
    import asyncio

    from ateam.allocation import rubric_text
    from ateam.contracts.store import save_contract
    from ateam.intent import commit_intent
    from ateam.planner import PlanError, render_plan_review, report_to_plan
    from ateam.plans.store import next_id as next_plan_id
    from ateam.plans.store import save_plan
    from ateam.records.store import load_records, save_record
    from ateam.roles import PLANNER_SPEC, Role
    from ateam.tiers import load_tiers

    ctx = _context(target)
    pid = next_plan_id(ctx.paths.plans)
    ctx.db.transition("feature", pid, "requested", by="owner", reason=request[:200])
    ctx.db.transition("feature", pid, "investigating", by="ateam")
    records = load_records(ctx.paths.records)
    tiers = load_tiers(ctx.config.tiers)
    brief = "\n\n".join([
        f"Feature request from the owner:\n{request}",
        _records_index(records),
        "Configured deterministic checks (use only these names): " + (", ".join(f"{c.name} (`{c.cmd}`)" for c in ctx.config.checks) or "none"),
        "Tier names: light, standard, careful; investigate may also be none.",
        "Allocation rubric:\n" + rubric_text(),
    ])
    result = asyncio.run(run_investigation(
        paths=ctx.paths, config=ctx.config, target_git=ctx.git, name=pid, spec=PLANNER_SPEC,
        prompt="Produce the plan for the feature request in your brief.", brief_text=brief,
        tier=tiers[tier][Role.investigate], db=ctx.db, tier_name=tier,
    ))

    def _rejected(reason: str) -> None:
        ctx.db.transition("feature", pid, "plan_proposed", by="ateam")
        ctx.db.transition("feature", pid, "plan_rejected", by="ateam", reason=reason)

    if result.tampered:
        _rejected("planner changed the checkout or intent repo")
        _fail("planning changed the checkout or the intent repo; report discarded: " + "; ".join(result.tampered))
    if result.res.report is None:
        _rejected("planner produced no report")
        _fail(f"planning produced no report (subtype {result.res.subtype}{', ' + result.res.error if result.res.error else ''})")
    try:
        pr = report_to_plan(result.res.report, request, pid, ctx.paths.contracts, records, ctx.config, ctx.paths.root, ctx.paths.records)
    except PlanError as e:
        _rejected("planner output invalid")
        click.echo("The planner's output is not usable; nothing was written. Problems:")
        for problem in e.problems:
            click.echo(f"  - {problem}")
        click.echo(f"Re-run: ateam plan \"{request}\"")
        sys.exit(1)
    for c in pr.contracts:
        save_contract(ctx.paths.contracts, c)
    for r in pr.proposed.proposed:
        save_record(ctx.paths.records, r)
    save_plan(ctx.paths.plans, pr.plan)
    commit_intent(ctx.paths, f"ateam: propose plan {pid}", ["plans", "contracts", "records"])
    ctx.db.transition("feature", pid, "plan_proposed", by="ateam")
    click.echo(render_plan_review(pr, tiers))
    click.echo(f"\nsession {result.res.session_id}: {result.res.num_turns} turns, ${result.res.cost_usd:.4f}")


def _dependency_order(contracts: dict) -> list[str]:
    """Contract ids with dependencies first; ties by id. Cycles were rejected at planning time."""
    remaining = dict(contracts)
    order: list[str] = []
    while remaining:
        ready = sorted(cid for cid, c in remaining.items() if all(d not in remaining for d in c.depends_on))
        if not ready:
            ready = sorted(remaining)[:1]
        for cid in ready:
            order.append(cid)
            remaining.pop(cid)
    return order


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
@TARGET_OPTION
def approve(plan_id: str, tier_specs: tuple[str, ...], skip_records: bool, target: Path) -> None:
    """Approve a plan: review its proposed records, apply tier overrides, freeze its contracts."""
    from datetime import UTC, datetime

    from ateam.allocation import investigate_tier_for
    from ateam.contracts.model import Level
    from ateam.contracts.select import freeze, select_records
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.intent import IntentError, commit_intent, require_clean
    from ateam.plans.model import PlanHistoryEntry, PlanStatus
    from ateam.plans.store import load_plans, save_plan
    from ateam.records.graph import RecordGraph
    from ateam.records.store import load_records
    from ateam.tiers import ORDER, at_least, load_tiers

    ctx = _context(target)
    plans_ = load_plans(ctx.paths.plans)
    if plan_id not in plans_:
        _fail(f"unknown plan {plan_id}")
    plan_ = plans_[plan_id]
    if plan_.status != PlanStatus.plan_proposed:
        _fail(f"{plan_id} is {plan_.status}; only plan_proposed plans can be approved")
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

    cs = load_contracts(ctx.paths.contracts)
    missing = [cid for cid in plan_.contracts if cid not in cs]
    if missing:
        _fail(f"{plan_id} names contracts that do not exist: {', '.join(missing)}")
    records = load_records(ctx.paths.records)
    graph = RecordGraph(records)
    notes: list[str] = []
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
        c = c.model_copy(update={"allocation": c.allocation.model_copy(update={"tier": tier})})
        selected = select_records(c.paths, graph, ctx.paths.root)
        frozen = freeze(c, selected)
        save_contract(ctx.paths.contracts, frozen)
        ctx.db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
        for r in selected:
            ctx.db.set_record_hash(r.id, r.content_hash())
        click.echo(f"  {frozen.id}  {frozen.status:<6} build={tier['build']} test={tier['test']} investigate={tier.get('investigate', 'none')}  records: {', '.join(r.id for r in selected) or 'none'}")
    for n in notes:
        click.echo(f"  note: {n}")
    plan_ = plan_.model_copy(update={
        "status": PlanStatus.plan_approved, "tier_overrides": overrides,
        "history": [*plan_.history, PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.plan_approved)],
    })
    save_plan(ctx.paths.plans, plan_)
    commit_intent(ctx.paths, f"ateam: approve plan {plan_id}", ["plans", "contracts"])
    ctx.db.transition("feature", plan_id, "plan_approved", by="owner")
    click.echo(f"{plan_id} -> plan_approved")


@main.command()
@click.argument("plan_id")
@click.argument("reason")
@TARGET_OPTION
def reject(plan_id: str, reason: str, target: Path) -> None:
    """Reject a proposed plan. Its contracts stay draft and are never frozen."""
    from datetime import UTC, datetime

    from ateam.intent import IntentError, commit_intent, require_clean
    from ateam.plans.model import PlanHistoryEntry, PlanStatus
    from ateam.plans.store import load_plans, save_plan

    ctx = _context(target)
    plans_ = load_plans(ctx.paths.plans)
    if plan_id not in plans_:
        _fail(f"unknown plan {plan_id}")
    plan_ = plans_[plan_id]
    if plan_.status != PlanStatus.plan_proposed:
        _fail(f"{plan_id} is {plan_.status}; only plan_proposed plans can be rejected")
    try:
        require_clean(ctx.paths)
    except IntentError as e:
        _fail(str(e))
    plan_ = plan_.model_copy(update={
        "status": PlanStatus.plan_rejected,
        "history": [*plan_.history, PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.plan_rejected, why=reason)],
    })
    save_plan(ctx.paths.plans, plan_)
    commit_intent(ctx.paths, f"ateam: reject plan {plan_id}", ["plans"])
    ctx.db.transition("feature", plan_id, "plan_rejected", by="owner", reason=reason)
    click.echo(f"{plan_id} -> plan_rejected")


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
    from ateam.records.lifecycle import LifecycleError
    from ateam.records.model import Basis
    from ateam.records.store import load_records

    ctx = _context(target)
    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    _review_records(ctx, [rid for rid, r in sorted(load_records(ctx.paths.records).items()) if review_all or r.status == "proposed"], counts)
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
    _print_overlaps(ctx, record_id)
    _lifecycle(ctx, lambda: _service(ctx).approve(record_id, Basis(basis) if basis else None))


@records.command("reject")
@click.argument("record_id")
@click.argument("reason")
@TARGET_OPTION
def records_reject(record_id: str, reason: str, target: Path) -> None:
    ctx = _context(target)
    _lifecycle(ctx, lambda: _service(ctx).reject(record_id, reason))


@records.command("supersede")
@click.argument("old_id")
@click.option("--with", "new_id", required=True, help="The proposed record that replaces it.")
@TARGET_OPTION
def records_supersede(old_id: str, new_id: str, target: Path) -> None:
    ctx = _context(target)
    _lifecycle(ctx, lambda: _service(ctx).supersede(old_id, new_id))


@records.command("retire")
@click.argument("record_id")
@click.argument("reason")
@TARGET_OPTION
def records_retire(record_id: str, reason: str, target: Path) -> None:
    ctx = _context(target)
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
    from ateam.records.graph import RecordGraph
    from ateam.records.store import load_records

    ctx = _context(target)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    if cs[contract_id].status != ContractStatus.draft and not force:
        _fail(f"{contract_id} is already frozen ({cs[contract_id].status}); pass --force to re-freeze and reset it")
    from ateam.intent import IntentError, commit_intent, require_clean

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
@TARGET_OPTION
def status(target: Path) -> None:
    """Feature and contract state from the operational store."""
    from ateam.plans.store import load_plans

    ctx = _context(target)
    plans_ = load_plans(ctx.paths.plans)
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
    from ateam.records.store import load_records
    from ateam.roles import Role

    ctx = _context(target)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    recs = load_records(ctx.paths.records)
    selected = [recs[ref.id] for ref in cs[contract_id].records if ref.id in recs]
    prev_raw = ctx.db.latest_checkpoint(contract_id)
    prev = Checkpoint.from_dict(prev_raw) if prev_raw else None
    click.echo(render_brief(assemble_brief(cs[contract_id], selected, prev, Role(role), scratch_dir=ctx.paths.scratch_root / contract_id)))


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
@TARGET_OPTION
def worker_run(contract_id: str, role: str, worktree: Path, base: str | None, tier: str | None, resume: str | None, prompt: str | None, target: Path) -> None:
    import asyncio
    import json

    from ateam.contracts.store import load_contracts
    from ateam.records.store import load_records
    from ateam.roles import Role
    from ateam.runner.run import default_base, run_contract_attempt
    from ateam.tiers import load_tiers

    from ateam.gitops import GitError
    from ateam.runner.binding import ContractNotBinding

    ctx = _context(target)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    contract = cs[contract_id]
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
    worktree = wt_git.root
    tiers = load_tiers(ctx.config.tiers)
    if tier and tier not in tiers:
        _fail(f"unknown tier {tier!r}; known: {', '.join(tiers)}")
    try:
        res, tree, scope, cp = asyncio.run(run_contract_attempt(
            paths=ctx.paths, config=ctx.config, git_worktree=wt_git, worktree=worktree,
            base=base or default_base(wt_git, ctx.config.target_branch),
            contract=contract, records=load_records(ctx.paths.records), role=Role(role), tiers=tiers,
            db=ctx.db, resume=resume, prompt=prompt, tier_override=tier,
        ))
    except ContractNotBinding as e:
        _fail(str(e), code=4)
    except ValueError as e:
        _fail(str(e))
    click.echo(f"session {res.session_id} subtype={res.subtype} turns={res.num_turns} cost=${res.cost_usd:.4f} wall={res.wall_s:.0f}s")
    if res.denials:
        click.echo("denials: " + "; ".join(res.denials))
    if res.error:
        click.echo(f"error: {res.error}")
    click.echo(f"scope ok={scope.ok} outside={scope.outside} dirty={scope.dirty} changed={tree.changed_vs_base}")
    click.echo(json.dumps(res.report, indent=2) if res.report else "no structured report")
    sys.exit(0 if (res.report and scope.ok) else (3 if res.report else 1))


if __name__ == "__main__":
    main()
