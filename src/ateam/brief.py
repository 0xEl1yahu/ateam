"""Brief assembly: four labelled sections, the only context a worker starts with."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from ateam.allocation import rubric_text
from ateam.checkpoint import Checkpoint
from ateam.config import TargetConfig
from ateam.contracts.model import Contract
from ateam.plans.model import Plan
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


DOC_CAP = 12_000
DEFAULT_MAX_CANDIDATES = 12


def _rules(target_root: Path, config: TargetConfig) -> str:
    parts: list[str] = []
    missing: list[str] = []
    for rel in ["CLAUDE.md", "AGENTS.md", *config.brief_docs]:
        f = target_root / rel
        if not f.is_file():
            if rel in config.brief_docs:
                missing.append(rel)
            continue
        text = f.read_text(errors="replace")
        if len(text) > DOC_CAP:
            text = text[:DOC_CAP] + f"\n[truncated at {DOC_CAP} characters]"
        parts.append(f"### {rel}\n{text.rstrip()}")
    if not parts:
        parts.append("No rules files found (CLAUDE.md, AGENTS.md, config brief_docs).")
    parts.extend(f"missing: {rel}" for rel in missing)
    return "\n\n".join(parts)


def _record_lines(records: dict[str, Record], status: str) -> str:
    rs = sorted((r for r in records.values() if r.status == status), key=lambda r: r.id)
    if not rs:
        return "none"
    return "\n".join(f"- {r.id} {r.kind} [{', '.join(r.binds.paths) or 'global'}] {r.statement.strip()}" for r in rs)


def _plans(plans: dict[str, Plan], contracts: dict[str, Contract]) -> str:
    if not plans:
        return "none"
    lines: list[str] = []
    for p in sorted(plans.values(), key=lambda p: p.id):
        lines.append(f"{p.id} {p.status}  {p.title}")
        for cid in p.contracts:
            c = contracts.get(cid)
            if c is not None:
                lines.append(f"  {c.id} {c.status}  {c.title}  [{', '.join(c.paths)}]")
    return "\n".join(lines)


def _demote_headings(text: str) -> str:
    """Push a document's own headings down two levels so they cannot be read as top-level brief sections."""
    return "\n".join("##" + line if line.startswith("#") else line for line in text.splitlines())


def assemble_investigation_brief(
    *,
    kind: Literal["discovery", "planning"],
    target_root: Path,
    config: TargetConfig,
    records: dict[str, Record],
    plans: dict[str, Plan],
    contracts: dict[str, Contract],
    scratch_dir: Path,
    request: str | None = None,
    max_candidates: int | None = None,
) -> str:
    """The brief an INVESTIGATE session starts with: the target's rules, records, plan index, checks and handoff path."""
    sections = [
        ("Rules", _rules(target_root, config)),
        (
            "Records",
            _record_lines(records, "approved")
            + "\n\nProposed, not yet approved (not binding; do not re-propose):\n"
            + _record_lines(records, "proposed"),
        ),
        ("Existing plans and contracts", _plans(plans, contracts)),
    ]
    if kind == "planning":
        sections.append(("Request", (request or "").strip()))
    else:
        n = DEFAULT_MAX_CANDIDATES if max_candidates is None else max_candidates
        sections.append(("Discovery limits", f"Propose at most {n} candidates; at most {config.global_record_cap} may be global."))
    sections += [
        ("Checks", "\n".join(f"{c.name}: {c.cmd}" for c in config.checks) or "none configured"),
        ("Tiers", "light, standard, careful; investigate may also be none."),
        ("Rubric", _demote_headings(rubric_text().strip())),
        ("Handoff file", str(scratch_dir / "handoff.yaml")),
    ]
    return "\n\n".join(f"## {h}\n{body}" for h, body in sections) + "\n"
