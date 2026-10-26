"""The ateam command-line interface."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ateam import SPIKED_CLI_VERSION, __version__
from ateam.brief import DEFAULT_MAX_CANDIDATES, assemble_investigation_brief
from ateam.config import TargetConfig, load_config
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir, lookup_target, normalise_remote, origin_url, record_target, repo_id, repo_id_source
from ateam.intent import IntentError, commit_intent, commit_intent_files, intent_relpath, require_clean
from ateam.runner.investigate import INTENT_TAMPER, run_investigation
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


@main.command()
@click.argument("request")
@click.option("--tier", default="careful", show_default=True, type=click.Choice(["light", "standard", "careful"]))
@click.option("--replaces", default=None, metavar="P-xxxx", help="Supersede this plan_proposed plan when the new plan is approved.")
@click.option("--no-review", "no_review", is_flag=True, help="Skip the staff-engineer review of the draft plan.")
@TARGET_OPTION
def plan(request: str, tier: str, replaces: str | None, no_review: bool, target: Path) -> None:
    """Turn a feature request into contracts and a plan for approval."""
    import asyncio

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
    if replaces is not None:
        old = load_plans(ctx.paths.plans).get(replaces)
        if old is None:
            _fail(f"--replaces names unknown plan {replaces}")
        if old.status != PlanStatus.plan_proposed:
            _fail(f"--replaces needs a plan_proposed plan; {replaces} is {old.status}")
    disk_next = int(next_plan_id(ctx.paths.plans).split("-")[1])
    db_max = ctx.db.max_feature_id()
    db_next = int(db_max.split("-")[1]) + 1 if db_max else 1
    pid = f"P-{max(disk_next, db_next):04d}"
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
        current = ctx.db.feature_status(pid)
        if current == "investigating":
            ctx.db.transition("feature", pid, "plan_proposed", by="ateam")
        if ctx.db.feature_status(pid) == "plan_proposed":
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
    except PlanError as e:
        _rejected("planner output invalid")
        click.echo("The planner's output is not usable; no contracts were written. Problems:")
        for problem in e.problems:
            click.echo(f"  - {problem}")
        click.echo(f"Re-run: ateam plan \"{request}\"")
        sys.exit(1)
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
                reason = "the session changed the checkout or intent repo: " + "; ".join(reviewed.tampered)
                if INTENT_TAMPER in reviewed.tampered:
                    intent_tampered_by = "review"
            elif report is None:
                reason = f"no report, subtype {review_res.subtype}"
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
            review = PlanReview(verdict="unavailable", session_id=review_res.session_id if review_res else None,
                                cost_usd=review_res.cost_usd if review_res else 0.0)
        progress["review"] = review
        if review.verdict != "needs_revision" or not any(f.severity == "blocking" for f in review.findings):
            return pr, review
        revision_note = ""
        prompt = ("A staff-engineer review found blocking problems with your plan. Revise the plan to address every "
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
@click.option("--force", is_flag=True, help="Approve a replacement whose replaced plan is no longer plan_proposed; nothing is superseded.")
@TARGET_OPTION
def approve(plan_id: str, tier_specs: tuple[str, ...], skip_records: bool, force: bool, target: Path) -> None:
    """Approve a plan: review its proposed records, apply tier overrides, freeze its contracts."""
    from datetime import UTC, datetime

    from ateam.allocation import investigate_tier_for, tier_for_complexity, tier_for_test
    from ateam.contracts.model import Level
    from ateam.contracts.select import freeze, select_records
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.gitops import GitError
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
    replacer = next((p for p in plans_.values() if p.replaces == plan_id and p.status == PlanStatus.plan_proposed), None)
    if replacer is not None:
        _fail(f"{plan_id} is replaced by proposed plan {replacer.id}; approve or reject {replacer.id} first")
    supersede = False   # the replaced plan is superseded only when it is still plan_proposed in both the file and the state store
    if plan_.replaces:
        old = plans_.get(plan_.replaces)
        file_status = str(old.status) if old is not None else "missing"
        store_status = ctx.db.feature_status(plan_.replaces) or "missing"
        if file_status == "plan_proposed" and store_status == "plan_proposed":
            supersede = True
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
    old_plan = plans_.get(plan_.replaces) if plan_.replaces else None
    if old_plan is not None and supersede:
        superseded_id = old_plan.id
        approval_files.append(save_plan(ctx.paths.plans, old_plan.model_copy(update={
            "status": PlanStatus.superseded,
            "history": [*old_plan.history, PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.superseded, why=f"replaced by {plan_id}")],
        })))
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
    if superseded_id is not None:
        if ctx.db.feature_status(superseded_id) == "plan_proposed":
            ctx.db.transition("feature", superseded_id, "superseded", by="owner", reason=f"replaced by {plan_id}")
            click.echo(f"{superseded_id} -> superseded")


@main.command()
@click.argument("plan_id")
@click.argument("reason")
@TARGET_OPTION
def reject(plan_id: str, reason: str, target: Path) -> None:
    """Reject a proposed plan. Its contracts stay draft and are never frozen."""
    from datetime import UTC, datetime

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
    commit_intent_files(ctx.paths, f"ateam: reject plan {plan_id}", [save_plan(ctx.paths.plans, plan_)])
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
    from ateam.records.store import load_records

    ctx = _context(target)
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
@TARGET_OPTION
def worker_run(contract_id: str, role: str, worktree: Path, base: str | None, tier: str | None, resume: str | None, prompt: str | None, target: Path) -> None:
    import asyncio
    import json

    from ateam.contracts.store import load_contracts
    from ateam.records.store import load_records
    from ateam.roles import Role
    from ateam.runner.run import SetupError, default_base, ensure_setup, run_contract_attempt
    from ateam.tiers import load_tiers

    from ateam.runner.binding import ContractNotBinding, require_binding

    ctx = _context(target)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    contract = cs[contract_id]
    records = load_records(ctx.paths.records)
    try:
        require_binding(contract, records, ctx.db)   # before setup: a contract that does not bind never provisions a worktree
    except ContractNotBinding as e:
        _fail(str(e), code=4)
    wt_git = _require_isolated_worktree(ctx, worktree)
    worktree = wt_git.root
    tiers = load_tiers(ctx.config.tiers)
    if tier and tier not in tiers:
        _fail(f"unknown tier {tier!r}; known: {', '.join(tiers)}")
    try:
        ensure_setup(worktree, ctx.config, ctx.db, on_command=lambda c: click.echo(f"setup: {c}"))
    except SetupError as e:
        _fail(f"setup failed: {e.cmd} (exit {e.returncode})\n{e.output}")
    try:
        res, tree, scope, cp = asyncio.run(run_contract_attempt(
            paths=ctx.paths, config=ctx.config, git_worktree=wt_git, worktree=worktree,
            base=base or default_base(wt_git, ctx.config.target_branch),
            contract=contract, records=records, role=Role(role), tiers=tiers,
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
    code = 0 if (res.report and scope.ok) else (3 if res.report else 1)
    if Role(role) is Role.test and res.report and scope.ok:
        from ateam.runner.run import classify_test_report

        test_outcome, test_class = classify_test_report(res.report)
        click.echo(f"classification: {test_outcome}/{test_class or '-'}")
        if test_outcome == "failed":
            code = 5   # a failed verdict (owner, 2026-10-26); 0 met, 3 scope breach, 1 no report, 4 refused
    click.echo(json.dumps(res.report, indent=2) if res.report else "no structured report")
    sys.exit(code)


# ---------------------------------------------------------------- verify (minimal)


@main.command()
@click.argument("contract_id")
@click.option("--cwd", "worktree", type=click.Path(file_okay=False, exists=True, path_type=Path), required=True, help="Worktree holding the committed work.")
@TARGET_OPTION
def verify(contract_id: str, worktree: Path, target: Path) -> None:
    """Minimal verification: the contract's checks, scope and acceptance tests, judged from the worktree.

    No TEST worker yet: judgment criteria are listed, not checked. Exit 0 verified, 1 failed or refused, 4 guard."""
    import json
    import os

    from ateam import verify as v
    from ateam.checkpoint import Checkpoint
    from ateam.contracts.model import ContractStatus
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.gitops import GitError
    from ateam.intent import intent_git
    from ateam.records.store import load_records
    from ateam.runner.binding import ContractNotBinding, require_binding
    from ateam.runner.run import default_base
    from ateam.treecheck import scope_check, tree_state

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
            sys.exit(1)
        set_file_status(contract, before_status, f"ateam: verify {contract_id} interrupted; back to {before_status}")
        ctx.db.delete_meta(pending_key, running_key)
        contract = load_contracts(ctx.paths.contracts)[contract_id]
        click.echo(f"note: an earlier verify of {contract_id} was interrupted before it was recorded; verifying again")

    if contract.status == ContractStatus.paused:
        _fail(f"{contract_id} is paused; the owner resumes it before it is verified", code=4)
    try:
        require_binding(contract, load_records(ctx.paths.records), ctx.db)
        require_clean(ctx.paths)
    except (ContractNotBinding, IntentError) as e:
        _fail(str(e), code=4)
    if not contract.checks:
        _fail(f"minimal verify needs at least one deterministic check; {contract_id} has none")
    base = default_base(wt_git, ctx.config.target_branch)
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
    sys.exit(0 if outcome.status == ContractStatus.verified else 1)


if __name__ == "__main__":
    main()
