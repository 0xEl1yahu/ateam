"""Brief assembly: four labelled sections, the only context a worker starts with."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from ateam.allocation import rubric_text
from ateam.checkpoint import Checkpoint
from ateam.config import TargetConfig
from ateam.contracts.model import Contract
from ateam.guidance import GuidanceEntry, live_guidance
from ateam.plans.model import Plan
from ateam.records.model import Record
from ateam.roles import Role


@dataclass
class Brief:
    binding: str
    task: str
    prior_work: str
    notes: str | None
    instructions: str | None = None


def render_record(r: Record) -> str:
    lines = [f"### ATeam binding record {r.id} ({r.kind})", r.statement.strip()]
    if r.rationale:
        lines.append(f"Why: {r.rationale.strip()}")
    lines.append("Binds: " + (", ".join(r.binds.paths) if r.binds.paths else "the whole repository"))
    if r.serves:
        lines.append("Serves: " + "; ".join(r.serves))
    return "\n".join(lines)


def _task(contract: Contract, scratch_dir: Path | None, role: Role | None = None) -> str:
    lines = [
        f"Contract {contract.id} ({contract.plan}): {contract.title}",
        "Paths you may change: " + ", ".join(contract.paths),
        "Incidental paths also allowed: " + (", ".join(contract.incidental) or "none"),
        "Depends on: " + (", ".join(contract.depends_on) or "nothing"),
    ]
    if scratch_dir is not None and role is Role.investigate:   # read-only: no handoff file to keep (owner, 2026-10-26)
        lines.append(f"Scratch directory: {scratch_dir}")
    elif scratch_dir is not None:
        lines.append(f"Handoff file: {scratch_dir / 'handoff.yaml'}  (your scratch directory: {scratch_dir})")
    lines += [
        "",
        "Acceptance criteria (read-only; you cannot change these):",
    ]
    for a in contract.acceptance:
        how = f"{a.check.type}" + (f" {a.check.ref}" if a.check.ref else "")
        lines.append(f"- {a.id}: {a.statement}  [{how}]")
    if contract.interfaces:
        lines.append("")
        lines.append("Interfaces (follow these exactly; other contracts depend on them):")
        lines += [f"- {i.kind}: {_one_line(i.signature)}" + (f"  ({_one_line(i.note)})" if i.note else "") for i in contract.interfaces]
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


INSTRUCTIONS_NOTE = ("Guidance from the owner for meeting this contract. It does not change the contract or any binding record; "
                     "if it ever seems to, the contract and records win.")


def render_instructions(entries: Sequence[GuidanceEntry]) -> str:
    lines = [INSTRUCTIONS_NOTE]
    for e in entries:
        who = "owner" if e.source == "owner" else f"adopted from investigation {e.source}"
        lines.append(f"- {e.at:%Y-%m-%d}, {who}: {_one_line(e.text)}")
    return "\n".join(lines)


def assemble_brief(
    contract: Contract, records: list[Record], checkpoint: Checkpoint | None, role: Role, scratch_dir: Path | None = None,
    instructions: Sequence[GuidanceEntry] = (),
) -> Brief:
    binding = "\n\n".join(render_record(r) for r in records) or "No binding records apply to this contract."
    notes = None
    if role != Role.test and checkpoint is not None and checkpoint.notes:
        notes = yaml.safe_dump(checkpoint.notes, sort_keys=False).strip()
    owner = render_instructions(instructions) if role is Role.build and instructions else None   # never TEST (owner, 2026-10-26)
    return Brief(binding=binding, task=_task(contract, scratch_dir, role), prior_work=_prior(checkpoint), notes=notes,
                 instructions=owner)


def render_brief(b: Brief) -> str:
    parts = [
        "## Binding\n" + b.binding,
        "## Task\n" + b.task,
        "## Verified prior work\n" + b.prior_work,
    ]
    if b.instructions:
        parts.append("## Owner's instructions\n" + b.instructions)
    if b.notes:
        parts.append("## Previous worker's notes (unverified)\n" + b.notes)
    return "\n\n".join(parts) + "\n"


DOC_CAP = 12_000
PAUSE_LOG_LINES = 60


def _pause_log(paused: dict, runs_dir: Path | None) -> str | None:
    """The tail of the paused step's log, read only when it resolves under runs_dir; None when the evidence has no log."""
    evidence = paused.get("evidence")
    if not isinstance(evidence, dict) or not evidence.get("log"):
        return None
    path = str(evidence["log"])
    try:
        if runs_dir is None:
            raise OSError("no runs directory")
        f = Path(path).resolve()
        if not f.is_relative_to(runs_dir.resolve()):
            raise OSError("outside runs")
        text = f.read_text(errors="replace")
    except OSError:
        return f"Note: could not read the paused step's log at {path}; its output is not shown."
    tail = "\n".join(text.splitlines()[-PAUSE_LOG_LINES:])
    if len(tail) > DOC_CAP:
        tail = tail[-DOC_CAP:]
    return f"Paused step's output (last {PAUSE_LOG_LINES} lines of {path}):\n{tail}"


def assemble_pause_brief(
    contract: Contract, records: list[Record], paused: dict, attempts: list[dict], build_checkpoint: Checkpoint | None,
    instructions: Sequence[GuidanceEntry], runs_dir: Path | None = None,
) -> str:
    """The brief for a pause investigation: what binds the contract, what it asks, why the plan paused (with the
    paused step's evidence and log tail), every attempt so far, the last BUILD worker's unverified notes, and the
    owner's live instructions already given."""
    import json

    binding = "\n\n".join(render_record(r) for r in records) or "No binding records apply to this contract."
    evidence = json.dumps(paused.get("evidence"), indent=2, default=str) if paused.get("evidence") is not None else "none recorded"
    if len(evidence) > DOC_CAP:
        evidence = evidence[:DOC_CAP] + f"\n[... truncated at {DOC_CAP} characters]"
    why = f"Reason: {paused.get('reason')}\nDetail: {_one_line(str(paused.get('detail') or ''))}\nEvidence:\n{evidence}"
    log = _pause_log(paused, runs_dir)
    if log is not None:
        why += "\n" + log
    history = "\n".join(
        f"- attempt {a.get('attempt_id')}: {a.get('role')} at {a.get('tier')}, {a.get('outcome') or 'unfinished'}"
        + (f" ({a['failure_class']})" if a.get("failure_class") else "")
        + (f", ${a['cost_usd']:.2f}" if a.get("cost_usd") is not None else "")
        for a in attempts) or "No attempts recorded."
    notes = (yaml.safe_dump(build_checkpoint.notes, sort_keys=False).strip()
             if build_checkpoint is not None and build_checkpoint.notes else "None.")
    live = live_guidance(list(instructions), contract.content_hash())
    given = render_instructions(live) if live else "None."
    sections = [("Binding", binding), ("Task", _task(contract, None, Role.investigate)), ("Why the plan paused", why),
                ("Attempt history", history), ("Previous worker's notes (unverified)", notes), ("Instructions already given", given)]
    return "\n\n".join(f"## {h}\n{body}" for h, body in sections) + "\n"

DEFAULT_MAX_CANDIDATES = 12


RULES_SCOPE_NOTE = (
    "These are the target's instructions for whoever develops it. They govern how work on the target is done; "
    "they are not requirements of the Request."
)


def _rules(target_root: Path, config: TargetConfig) -> str:
    parts: list[str] = []
    missing: list[str] = []
    root = target_root.resolve()
    for rel in dict.fromkeys(["CLAUDE.md", "AGENTS.md", *config.brief_docs]):
        f = (target_root / rel).resolve()
        if not f.is_relative_to(root):
            if rel in config.brief_docs:
                missing.append(f"{rel} (outside the target)")
            continue
        if not f.is_file():
            if rel in config.brief_docs:
                missing.append(rel)
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            missing.append(f"{rel} (unreadable)")
            continue
        marker = ""
        if len(text) > DOC_CAP:
            text, marker = text[:DOC_CAP], f"\n[truncated at {DOC_CAP} characters]"
        parts.append(f"### {rel}\n{_demote_headings(text, 3).rstrip()}{marker}")
    if not parts:
        parts.append("No rules files found (CLAUDE.md, AGENTS.md, config brief_docs).")
    parts.extend(f"missing: {rel}" for rel in missing)
    return RULES_SCOPE_NOTE + "\n\n" + "\n\n".join(parts)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _record_lines(records: dict[str, Record], status: str) -> str:
    rs = sorted((r for r in records.values() if r.status == status), key=lambda r: r.id)
    if not rs:
        return "none"
    return "\n".join(f"- {r.id} {r.kind} [{', '.join(r.binds.paths) or 'global'}] {_one_line(r.statement)}" for r in rs)


PLAN_INDEX_CAP = 2500


def _plan_block(p: Plan, contracts: dict[str, Contract]) -> str:
    lines = [f"{p.id} {p.status}  {_one_line(p.title)}"]
    if p.unknowns:
        lines.append("  unknowns:")
        lines += [f"    - {_one_line(u)}" for u in p.unknowns]
    if p.verification_approach.strip():
        lines.append("  verification: " + _one_line(p.verification_approach)[:80])
    for cid in p.contracts:
        c = contracts.get(cid)
        if c is None:
            continue
        lines.append(f"  {c.id} {c.status}  {_one_line(c.title)}")
        lines.append(f"    paths: {', '.join(c.paths)}")
        lines += [f"    {a.id}: {_one_line(a.statement)}" for a in c.acceptance]
        lines += [f"    {i.kind}: {_one_line(i.signature)}" for i in c.interfaces]
    text = "\n".join(lines)
    if len(text) > PLAN_INDEX_CAP:
        text = text[:PLAN_INDEX_CAP] + f"\n[truncated at {PLAN_INDEX_CAP} characters]"
    return text


def _plans(plans: dict[str, Plan], contracts: dict[str, Contract], exclude: set[str] = frozenset()) -> str:
    shown = sorted((p for p in plans.values() if p.id not in exclude), key=lambda p: p.id)
    if not shown:
        return "none"
    return "\n".join(_plan_block(p, contracts) for p in shown)


def _demote_headings(text: str, levels: int = 2) -> str:
    """Push a document's own headings down so they cannot be read as top-level brief sections."""
    return "\n".join("#" * levels + line if line.startswith("#") else line for line in text.splitlines())


def _context_sections(
    target_root: Path, config: TargetConfig, records: dict[str, Record], plans: dict[str, Plan],
    contracts: dict[str, Contract], exclude_plans: set[str],
) -> list[tuple[str, str]]:
    return [
        ("Rules", _rules(target_root, config)),
        (
            "Records",
            "Approved (binding; do not re-propose):\n"
            + _record_lines(records, "approved")
            + "\n\nProposed, not yet approved (not binding; do not re-propose):\n"
            + _record_lines(records, "proposed"),
        ),
        ("Existing plans and contracts", _plans(plans, contracts, exclude_plans)),
    ]


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
    exclude_plans: set[str] = frozenset(),
) -> str:
    """The brief an INVESTIGATE session starts with: the target's rules, records, plan index, checks and handoff path."""
    sections = _context_sections(target_root, config, records, plans, contracts, exclude_plans)
    if kind == "planning":
        sections.append(("Request", _demote_headings((request or "").strip())))
    else:
        n = DEFAULT_MAX_CANDIDATES if max_candidates is None else max_candidates
        sections.append(("Discovery limits", f"Propose at most {n} candidates; at most {config.global_record_cap} may be global."))
    sections += [
        ("Checks", "\n".join(f"{c.name}: {c.cmd}" for c in config.checks) or "none configured"),
        ("Tiers", "light, standard, careful; investigate may also be none."),
        ("Rubric", _demote_headings(rubric_text().strip())),
    ]
    return "\n\n".join(f"## {h}\n{body}" for h, body in sections) + "\n"


def assemble_review_brief(
    *,
    target_root: Path,
    config: TargetConfig,
    records: dict[str, Record],
    plans: dict[str, Plan],
    contracts: dict[str, Contract],
    scratch_dir: Path,
    request: str,
    draft: str,
    exclude_plans: set[str] = frozenset(),
) -> str:
    """The brief for the staff-engineer review: the planner's context, the draft plan, and no Tiers section."""
    sections = _context_sections(target_root, config, records, plans, contracts, exclude_plans)
    sections += [
        ("Request", _demote_headings(request.strip())),
        ("Draft plan", _demote_headings(draft.strip())),
        ("Checks", "\n".join(f"{c.name}: {c.cmd}" for c in config.checks) or "none configured"),
        ("Rubric", _demote_headings(rubric_text().strip())),
    ]
    return "\n\n".join(f"## {h}\n{body}" for h, body in sections) + "\n"
