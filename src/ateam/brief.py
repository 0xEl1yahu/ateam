"""Brief assembly: four labelled sections, the only context a worker starts with."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ateam.checkpoint import Checkpoint
from ateam.contracts.model import Contract
from ateam.records.model import Record
from ateam.roles import Role


@dataclass
class Brief:
    binding: str
    task: str
    prior_work: str
    notes: str | None


def render_record(r: Record) -> str:
    lines = [f"### ATeam binding record {r.id} ({r.kind})", r.statement.strip()]
    if r.rationale:
        lines.append(f"Why: {r.rationale.strip()}")
    lines.append("Binds: " + (", ".join(r.binds.paths) if r.binds.paths else "the whole repository"))
    if r.serves:
        lines.append("Serves: " + "; ".join(r.serves))
    return "\n".join(lines)


def _task(contract: Contract, scratch_dir: Path | None) -> str:
    lines = [
        f"Contract {contract.id} ({contract.plan}): {contract.title}",
        "Paths you may change: " + ", ".join(contract.paths),
        "Incidental paths also allowed: " + (", ".join(contract.incidental) or "none"),
        "Depends on: " + (", ".join(contract.depends_on) or "nothing"),
    ]
    if scratch_dir is not None:
        lines.append(f"Handoff file: {scratch_dir / 'handoff.yaml'}  (your scratch directory: {scratch_dir})")
    lines += [
        "",
        "Acceptance criteria (read-only; you cannot change these):",
    ]
    for a in contract.acceptance:
        how = f"{a.check.type}" + (f" {a.check.ref}" if a.check.ref else "")
        lines.append(f"- {a.id}: {a.statement}  [{how}]")
    if contract.checks:
        lines.append("")
        lines.append("Deterministic checks:")
        lines.extend(f"- {c.name}: `{c.cmd}`" for c in contract.checks)
    b = contract.budget
    lines.append("")
    lines.append(f"Budget: {b.attempts} attempts, {b.max_turns} turns and {b.usd_per_attempt:.2f} USD per attempt.")
    return "\n".join(lines)


def _prior(cp: Checkpoint | None) -> str:
    if cp is None:
        return "This is the first attempt at this contract. No prior work exists."
    lines = [f"Worktree base commit: {cp.base}", f"Current HEAD: {cp.head}"]
    lines.append("Commits since base:" if cp.commits else "Commits since base: none")
    lines.extend(f"- {c}" for c in cp.commits)
    lines.append("Files changed vs base: " + (", ".join(cp.diff_names) or "none"))
    if cp.dirty:
        lines.append("Uncommitted paths: " + ", ".join(p for _, p in cp.dirty))
    if cp.checks:
        lines.append("Last check results:")
        lines.extend(f"- {c.get('name')}: {'passed' if c.get('passed') else 'FAILED'}" for c in cp.checks)
    return "\n".join(lines)


def assemble_brief(
    contract: Contract, records: list[Record], checkpoint: Checkpoint | None, role: Role, scratch_dir: Path | None = None
) -> Brief:
    binding = "\n\n".join(render_record(r) for r in records) or "No binding records apply to this contract."
    notes = None
    if role != Role.test and checkpoint is not None and checkpoint.notes:
        notes = yaml.safe_dump(checkpoint.notes, sort_keys=False).strip()
    return Brief(binding=binding, task=_task(contract, scratch_dir), prior_work=_prior(checkpoint), notes=notes)


def render_brief(b: Brief) -> str:
    parts = [
        "## Binding\n" + b.binding,
        "## Task\n" + b.task,
        "## Verified prior work\n" + b.prior_work,
    ]
    if b.notes:
        parts.append("## Previous worker's notes (unverified)\n" + b.notes)
    return "\n\n".join(parts) + "\n"
