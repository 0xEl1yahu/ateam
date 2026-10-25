"""The ateam command-line interface."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ateam import SPIKED_CLI_VERSION, __version__
from ateam.config import TargetConfig, load_config
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir
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


def _context(target: Path, require_ateam: bool = True) -> Context:
    if require_ateam and not TargetPaths.from_root(target).exists():
        click.echo(f"No .ateam/ directory in {target}. Create .ateam/records/ and .ateam/contracts/ (see README, Your first record and contract); ateam init arrives in pass two.")
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

    return RecordService(ctx.paths, ctx.git, ctx.config)


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
    graph = RecordGraph(load_records(ctx.paths.records))
    selected = select_records(cs[contract_id].paths, graph, ctx.paths.root)
    frozen = freeze(cs[contract_id], selected)
    save_contract(ctx.paths.contracts, frozen)
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
    ctx = _context(target)
    rows = ctx.db.conn.execute("SELECT feature_id, status FROM features ORDER BY feature_id").fetchall()
    click.echo("Features:" if rows else "Features: none")
    for r in rows:
        click.echo(f"  {r['feature_id']}  {r['status']}")
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
    click.echo(f"target {target}: .ateam/ {'present' if paths.exists() else 'missing'}")
    try:
        git = GitRepo.discover(target)
        cfg = load_config(paths)
        click.echo(f"branch {git.current_branch()} (target branch {cfg.target_branch})")
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
    click.echo(render_brief(assemble_brief(cs[contract_id], selected, prev, Role(role))))


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
