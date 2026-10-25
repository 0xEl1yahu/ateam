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
        click.echo(f"No .ateam/ directory in {target}. Run: ateam init")
        sys.exit(2)
    return Context(target)


def _fail(message: str, code: int = 1) -> None:
    click.echo(f"error: {message}")
    sys.exit(code)


@click.group()
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
    from ateam.records.store import load_records
    from ateam.records.validate import validate_records

    ctx = _context(target)
    problems = validate_records(load_records(ctx.paths.records), ctx.paths.root, ctx.db.record_hashes())
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
@TARGET_OPTION
def contracts_freeze(contract_id: str, target: Path) -> None:
    from ateam.contracts.select import freeze, select_records
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.records.graph import RecordGraph
    from ateam.records.store import load_records

    ctx = _context(target)
    cs = load_contracts(ctx.paths.contracts)
    if contract_id not in cs:
        _fail(f"unknown contract {contract_id}")
    graph = RecordGraph(load_records(ctx.paths.records))
    selected = select_records(cs[contract_id].paths, graph, ctx.paths.root)
    frozen = freeze(cs[contract_id], selected)
    save_contract(ctx.paths.contracts, frozen)
    ctx.db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
    for r in selected:
        ctx.db.set_record_hash(r.id, r.content_hash())
    click.echo(f"{frozen.id} frozen as {frozen.status} with records: {', '.join(r.id for r in selected) or '(none)'}")


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


if __name__ == "__main__":
    main()
