"""Planner: turning a planner worker's report into draft contracts and a plan, after validating all of it.

Nothing is written when anything fatal is wrong (dependency cycles, unknown checks, invalid globs).
Allocation corrections the rubric requires are applied and recorded as notes, never silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ateam.allocation import check_allocation, default_tiers, investigate_tier_for
from ateam.config import TargetConfig
from ateam.contracts.model import Acceptance, Allocation, Budget, Contract, ContractStatus, Level
from ateam.contracts.select import select_records
from ateam.contracts.store import next_id as next_contract_id
from ateam.discovery import DiscoveryResult, candidates_to_records, render_discovery_report
from ateam.globs import is_valid_pattern, matches
from ateam.plans.model import Plan, PlanHistoryEntry, PlanStatus
from ateam.records.graph import RecordGraph
from ateam.records.model import Record
from ateam.roles import Role
from ateam.tiers import ORDER, at_least


class PlanError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class PlanResult:
    plan: Plan
    contracts: list[Contract]
    proposed: DiscoveryResult
    problems: list[str] = field(default_factory=list)
    bound_preview: dict[str, list[str]] = field(default_factory=dict)


def _has_cycle(raw: list[dict]) -> bool:
    n = len(raw)
    indeg = [0] * n
    out: list[list[int]] = [[] for _ in range(n)]
    for i, c in enumerate(raw):
        for d in c.get("depends_on", []):
            if 0 <= d < n:
                out[d].append(i)
                indeg[i] += 1
    queue = [i for i in range(n) if indeg[i] == 0]
    seen = 0
    while queue:
        i = queue.pop()
        seen += 1
        for j in out[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                queue.append(j)
    return seen != n


def report_to_plan(
    report: dict, request: str, plan_id: str, contracts_dir: Path, records: dict[str, Record], config: TargetConfig,
    repo_root: Path, records_dir: Path, by: str = "ateam-plan",
) -> PlanResult:
    raw = list(report.get("contracts") or [])
    fatal: list[str] = []
    notes: list[str] = []
    known_checks = {c.name: c for c in config.checks}
    if not raw:
        fatal.append("the plan has no contracts")
    for i, c in enumerate(raw):
        title = c.get("title") or f"contract #{i}"
        for d in c.get("depends_on", []):
            if not isinstance(d, int) or not (0 <= d < len(raw)) or d == i:
                fatal.append(f"{title}: depends_on index {d} is out of range")
        for name in c.get("checks", []):
            if name not in known_checks:
                fatal.append(f"{title}: unknown check {name!r}; configured: {', '.join(known_checks) or 'none'}")
        if not c.get("paths"):
            fatal.append(f"{title}: no paths declared")
        for g in [*c.get("paths", []), *c.get("incidental", [])]:
            if not is_valid_pattern(g):
                fatal.append(f"{title}: invalid glob {g!r}")
    if raw and _has_cycle(raw):
        fatal.append("depends_on forms a cycle")
    if fatal:
        raise PlanError(fatal)

    start = int(next_contract_id(contracts_dir).split("-")[1])
    ids = [f"C-{start + i:04d}" for i in range(len(raw))]
    contracts: list[Contract] = []
    for i, c in enumerate(raw):
        cid = ids[i]
        try:
            alloc = Allocation.model_validate(c["allocation"])
            acceptance = [Acceptance.model_validate(a) for a in c.get("acceptance", [])]
            budget = Budget.model_validate(c["budget"]) if c.get("budget") else Budget()
        except (ValidationError, KeyError) as e:
            raise PlanError([f"{cid} ({c.get('title', '?')}): {str(e).splitlines()[0]}"]) from e
        defaults = default_tiers(alloc)
        tier = dict(alloc.tier)
        if tier.get("build") not in ORDER:
            notes.append(f"{cid}: build tier {tier.get('build')!r} unknown; set to {defaults['build']!r} from complexity")
            tier["build"] = defaults["build"]
        test = tier.get("test")
        wanted = at_least(tier["build"], test if test in ORDER else "light")
        if wanted != test:
            notes.append(f"{cid}: test tier raised from {test!r} to {wanted!r} (TEST never runs below BUILD)")
            tier["test"] = wanted
        inv = tier.get("investigate", "none")
        if inv == "none" and Level(alloc.uncertainty) in (Level.medium, Level.high):
            tier["investigate"] = investigate_tier_for(alloc.uncertainty)
            notes.append(f"{cid}: investigate tier set to {tier['investigate']!r} for {alloc.uncertainty} uncertainty")
        elif inv != "none" and inv not in ORDER:
            tier["investigate"] = defaults["investigate"]
            notes.append(f"{cid}: investigate tier {inv!r} unknown; set to {tier['investigate']!r}")
        alloc = alloc.model_copy(update={"tier": tier})
        notes.extend(f"{cid}: {p}" for p in check_allocation(alloc))
        scope = [*c["paths"], *c.get("incidental", [])]
        for a in acceptance:
            if a.check.type == "test" and a.check.ref:
                file_part = a.check.ref.split("::")[0]
                if not any(matches(g, file_part) for g in scope):
                    notes.append(f"{cid}: acceptance {a.id} names {file_part}, outside the contract's paths; a BUILD worker could not write it")
        contracts.append(Contract(
            id=cid, plan=plan_id, title=c["title"], status=ContractStatus.draft,
            depends_on=[ids[d] for d in c.get("depends_on", [])], paths=list(c["paths"]), incidental=list(c.get("incidental", [])),
            acceptance=acceptance, checks=[known_checks[n] for n in c.get("checks", [])], allocation=alloc, budget=budget,
        ))

    proposed = candidates_to_records(
        list(report.get("proposed_records") or []), list(report.get("unknowns") or []), records, repo_root, records_dir,
        cap_global=config.global_record_cap, by=by,
    )
    graph = RecordGraph(records)
    bound_preview = {c.id: [r.id for r in select_records(c.paths, graph, repo_root)] for c in contracts}
    plan = Plan(
        id=plan_id, title=(report.get("title") or request)[:80], request=request, status=PlanStatus.plan_proposed,
        contracts=ids, proposed_records=[r.id for r in proposed.proposed], unknowns=list(report.get("unknowns") or []),
        history=[PlanHistoryEntry(at=datetime.now(UTC), by=by, to=PlanStatus.plan_proposed)],
    )
    return PlanResult(plan=plan, contracts=contracts, proposed=proposed, problems=notes, bound_preview=bound_preview)


def render_plan_review(result: PlanResult, tiers) -> str:
    p = result.plan
    lines = [f"Plan {p.id}: {p.title}", f"Request: {p.request}", ""]
    for c in result.contracts:
        a = c.allocation
        lines.append(f"{c.id}  {c.title}")
        lines.append("  paths: " + ", ".join(c.paths) + (f"  (incidental: {', '.join(c.incidental)})" if c.incidental else ""))
        lines.append("  depends on: " + (", ".join(c.depends_on) or "nothing"))
        for acc in c.acceptance:
            how = acc.check.type + (f" {acc.check.ref}" if acc.check.ref else "")
            lines.append(f"  - {acc.id}: {acc.statement}  [{how}]")
        lines.append("  checks: " + (", ".join(ch.name for ch in c.checks) or "none"))
        lines.append(f"  complexity: {a.complexity}   risk: {a.risk}   uncertainty: {a.uncertainty}")
        lines.append(f"  reasoning: {a.reasoning.strip()}")
        parts = []
        for role in ("build", "test", "investigate"):
            name = a.tier.get(role, "none")
            model = tiers[name][Role(role)].model if name in tiers else "-"
            parts.append(f"{role}: {name}" + (f" ({model})" if name in tiers else ""))
        lines.append("  tiers: " + "   ".join(parts))
        bound = result.bound_preview.get(c.id, [])
        lines.append("  bound records: " + (", ".join(bound) if bound else "none (no approved records apply)"))
        lines.append("")
    if result.proposed.proposed or result.proposed.dropped or result.proposed.skipped_rejected:
        lines.append("Records proposed by this plan:")
        lines.append(render_discovery_report(result.proposed).replace("\nNext: ateam records review   (approve as requirement or observation, reject with a reason, or skip)", ""))
        lines.append("")
    if p.unknowns:
        lines.append("Open questions:")
        lines.extend(f"  - {u}" for u in p.unknowns)
        lines.append("")
    if result.problems:
        lines.append("Notes (corrections applied or warnings):")
        lines.extend(f"  - {n}" for n in result.problems)
        lines.append("")
    first = result.contracts[0].id if result.contracts else "C-0001"
    lines.append(f"Approve: ateam approve {p.id} [--tier {first}=careful] [--tier {first}:test=careful] [--skip-records]")
    lines.append(f"Reject:  ateam reject {p.id} \"reason\"")
    return "\n".join(lines)
