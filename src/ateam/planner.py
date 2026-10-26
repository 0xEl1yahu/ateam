"""Planner: turning a planner worker's report into draft contracts and a plan, after validating all of it.

Nothing is written when anything fatal is wrong (dependency cycles, unknown checks, invalid globs).
Allocation corrections the rubric requires are applied and recorded as notes, never silently.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from ateam.allocation import budget_for, check_allocation, default_tiers, investigate_tier_for, tier_for_complexity, tier_for_test
from ateam.config import TargetConfig
from ateam.contracts.model import Acceptance, Allocation, Budget, Contract, ContractStatus, Interface, Level
from ateam.contracts.select import select_records
from ateam.contracts.store import next_id as next_contract_id
from ateam.discovery import DiscoveryResult, candidates_to_records, render_discovery_report
from ateam.globs import is_valid_pattern, matches
from ateam.plans.model import Plan, PlanHistoryEntry, PlanReview, PlanStatus, ReviewFinding
from ateam.records.graph import RecordGraph
from ateam.records.model import Record
from ateam.roles import Role
from ateam.tiers import ORDER, at_least, load_tiers


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
    covered: bool = False


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


_TEXT_SEARCH_CAP = 2_000_000


def _python_definitions(text: str, path: Path) -> set[str]:
    """Names defined in one Python file, with class and module qualification."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    names: set[str] = set()
    modules = list(path.with_suffix("").parts)
    if modules[-1] == "__init__":
        modules.pop()
    prefixes = ["", *(".".join(modules[i:]) + "." for i in range(len(modules)))]

    def visit(body: list[ast.stmt], scope: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = scope + node.name
                names.update(prefix + name for prefix in prefixes)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, name + ".")

    visit(tree.body)
    return names


def _search_interfaces(repo_root: Path, globs: list[str], files: list[str]) -> tuple[str, set[str]]:
    """Bounded text and Python definitions in declared/read files; not an exhaustive code index."""
    seen: dict[Path, None] = {}
    root = repo_root.resolve()
    for g in globs:
        pattern = g[:-2] + "**/*" if g.endswith("**") else g
        try:
            for f in root.glob(pattern):
                resolved = f.resolve()
                if resolved.is_relative_to(root) and resolved.is_file():
                    seen[resolved] = None
        except (ValueError, NotImplementedError, OSError):
            continue
    for rel in files:
        f = (root / rel).resolve()
        if f.is_relative_to(root) and f.is_file():
            seen[f] = None
    parts: list[str] = []
    definitions: set[str] = set()
    remaining = _TEXT_SEARCH_CAP
    for f in seen:
        try:
            with f.open("rb") as stream:
                data = stream.read(remaining + 1)
        except OSError:
            continue
        if len(data) > remaining:
            break
        remaining -= len(data)
        text = data.decode(errors="replace")
        parts.append(text)
        if f.suffix == ".py":
            definitions.update(_python_definitions(text, f.relative_to(root)))
    return "\n".join(parts), definitions


_SYMBOL = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
_GROUPED_SYMBOLS = re.compile(rf"{_SYMBOL}(?:(?:\s*,\s*|\s+/\s+){_SYMBOL})+")


def _interface_notes(raw: list[dict], ids: list[str], interfaces: list[list[Interface]], repo_root: Path, files_read: list[str]) -> list[str]:
    notes: list[str] = []
    produced: dict[str, tuple[str, str]] = {}   # name -> (contract id, signature), from earlier contracts only
    for i, ifs in enumerate(interfaces):
        text: str | None = None
        definitions: set[str] = set()
        for itf in ifs:
            if itf.kind != "consumes":
                continue
            if _GROUPED_SYMBOLS.fullmatch(itf.name.strip()):
                notes.append(f"{ids[i]} consumes `{itf.name}`: declare one interface per entry with its exact signature")
                continue
            prior = produced.get(itf.name)
            if prior is not None:
                if " ".join(prior[1].split()) != " ".join(itf.signature.split()):
                    notes.append(f"{ids[i]} consumes `{itf.signature}` but {prior[0]} produces `{prior[1]}`")
                continue
            if text is None:
                scope = [*raw[i].get("paths", []), *raw[i].get("incidental", [])]
                text, definitions = _search_interfaces(repo_root, scope, files_read)
            if itf.name not in definitions and itf.name not in text:
                notes.append(f"{ids[i]} consumes `{itf.name}`: could not verify it in earlier contracts or the searched files; "
                             "check the name and include its defining file in files_read")
        for itf in ifs:
            if itf.kind == "produces":
                produced[itf.name] = (ids[i], itf.signature)
    return notes


_CONTRACT_REF = re.compile(r"\bcontract (\d+)\b(?![.\-]\d)", re.IGNORECASE)
_OTHER_PLAN = re.compile(r"(P-\d{4}'s|\bplan(?:'s)?)\s+$", re.IGNORECASE)   # "P-0002's contract 1", "plan contract 1"


def _resolve_one(text: str, ids: list[str]) -> str:
    def repl(m: re.Match) -> str:
        n = int(m.group(1))
        before = text[max(0, m.start() - 16):m.start()]
        another_plan = bool(_OTHER_PLAN.search(before)) and "this plan" not in before
        return m.group(0) if n >= len(ids) or another_plan else ids[n]
    return _CONTRACT_REF.sub(repl, text)


def resolve_contract_refs(value, ids: list[str]):
    """`contract N` (zero-based, as the planner writes it before ids exist) becomes the contract's id; an index outside
    the plan, a reference to another plan's contract, and prose that merely mentions a contract are left as written."""
    if isinstance(value, str):
        return _resolve_one(value, ids)
    if isinstance(value, list):
        return [resolve_contract_refs(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: resolve_contract_refs(v, ids) for k, v in value.items()}
    return value


def _resolve_prose(report: dict, ids: list[str]) -> dict:
    """Apply resolve_contract_refs to the report's prose fields only."""
    out = dict(report)
    for key in ("title", "unknowns", "notes", "out_of_scope", "verification_approach"):
        if key in out:
            out[key] = resolve_contract_refs(out[key], ids)
    out["proposed_records"] = [{**r, **{k: resolve_contract_refs(r[k], ids) for k in ("statement", "rationale") if k in r}}
                               for r in (report.get("proposed_records") or [])]
    contracts = []
    for c in report.get("contracts") or []:
        c = dict(c)
        if "title" in c:
            c["title"] = resolve_contract_refs(c["title"], ids)
        c["acceptance"] = [{**a, "statement": resolve_contract_refs(a.get("statement", ""), ids)} for a in c.get("acceptance") or []]
        c["interfaces"] = [{**i, **({"note": resolve_contract_refs(i["note"], ids)} if i.get("note") else {})} for i in c.get("interfaces") or []]
        if isinstance(c.get("allocation"), dict) and "reasoning" in c["allocation"]:
            c["allocation"] = {**c["allocation"], "reasoning": resolve_contract_refs(c["allocation"]["reasoning"], ids)}
        contracts.append(c)
    out["contracts"] = contracts
    return out


def report_to_plan(
    report: dict, request: str, plan_id: str, contracts_dir: Path, records: dict[str, Record], config: TargetConfig,
    repo_root: Path, records_dir: Path, by: str = "ateam-plan",
    tiers: dict | None = None, existing_plans: dict[str, Plan] | None = None, replaces: str | None = None,
) -> PlanResult:
    raw = list(report.get("contracts") or [])
    fatal: list[str] = []
    notes: list[str] = []
    known_checks = {c.name: c for c in config.checks}
    if not raw:
        dead_states = (PlanStatus.plan_rejected, PlanStatus.plan_covered, PlanStatus.superseded, PlanStatus.abandoned)
        live = {pid for pid, p in (existing_plans or {}).items() if p.status not in dead_states and pid != replaces}
        named = list(dict.fromkeys(report.get("depends_on_plans") or []))
        if named and all(d in live and d != plan_id for d in named):
            plan = Plan(
                id=plan_id, title=(report.get("title") or request)[:80], request=request, status=PlanStatus.plan_covered,
                covered_by=named, unknowns=list(report.get("unknowns") or []), files_read=list(report.get("files_read") or []),
                out_of_scope=list(report.get("out_of_scope") or []), verification_approach=report.get("verification_approach") or "",
                replaces=replaces, history=[PlanHistoryEntry(at=datetime.now(UTC), by=by, to=PlanStatus.plan_covered)],
            )
            if report.get("notes"):
                notes.append(str(report["notes"]))
            return PlanResult(plan=plan, contracts=[], proposed=DiscoveryResult(unknowns=list(plan.unknowns)), problems=notes, covered=True)
        fatal.append("the plan has no contracts")
    for i, c in enumerate(raw):
        title = c.get("title") or f"contract #{i}"
        for d in c.get("depends_on", []):
            if isinstance(d, int) and d == i:
                fatal.append(f"{title}: depends on itself")
            elif not isinstance(d, int) or not (0 <= d < len(raw)):
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

    tiers = tiers or load_tiers()
    existing_plans = existing_plans or {}
    depends_on_plans: list[str] = []
    for dep in dict.fromkeys(report.get("depends_on_plans") or []):
        if dep == plan_id:
            notes.append(f"depends_on_plans named this plan's own id {plan_id}; dropped")
        else:
            depends_on_plans.append(dep)
            if dep not in existing_plans:
                notes.append(f"depends_on_plans names unknown plan {dep}")

    mentioned = re.findall(r"P-\d{4}", " ".join([*(report.get("unknowns") or []), str(report.get("notes") or "")]))
    for pid in dict.fromkeys(mentioned):
        if pid in existing_plans and pid not in depends_on_plans and pid != plan_id:
            notes.append(f"unknowns mention {pid}, which is not in depends_on_plans")

    start = int(next_contract_id(contracts_dir).split("-")[1])
    ids = [f"C-{start + i:04d}" for i in range(len(raw))]
    report = _resolve_prose(report, ids)   # prose fields only: paths, checks, refs and signatures are left as written
    raw = list(report.get("contracts") or [])
    contracts: list[Contract] = []
    all_interfaces: list[list[Interface]] = []
    for i, c in enumerate(raw):
        cid = ids[i]
        try:
            alloc = Allocation.model_validate(c["allocation"])
            acceptance = [Acceptance.model_validate(a) for a in c.get("acceptance", [])]
            given_budget = Budget.model_validate(c["budget"]) if c.get("budget") else None
            interfaces = [Interface.model_validate(x) for x in c.get("interfaces", [])]
        except (ValidationError, KeyError) as e:
            raise PlanError([f"{cid} ({c.get('title', '?')}): {str(e).splitlines()[0]}"]) from e
        defaults = default_tiers(alloc)
        tier = dict(alloc.tier)
        if tier.get("build") not in ORDER:
            notes.append(f"{cid}: build tier {tier.get('build')!r} unknown; set to {defaults['build']!r} from complexity")
            tier["build"] = defaults["build"]
        floor = tier_for_complexity(alloc.complexity)
        if at_least(tier["build"], floor) != tier["build"]:
            notes.append(f"{cid}: build tier raised from {tier['build']!r} to {floor!r} ({alloc.complexity} complexity, per the rubric)")
            tier["build"] = floor
        test = tier.get("test")
        wanted = at_least(tier_for_test(alloc.risk, tier["build"]), test if test in ORDER else "light")
        if wanted != test:
            why = "TEST never runs below BUILD" if wanted == tier["build"] and alloc.risk == Level.low else f"{alloc.risk} risk, per the rubric"
            notes.append(f"{cid}: test tier raised from {test!r} to {wanted!r} ({why})")
            tier["test"] = wanted
        inv = tier.get("investigate", "none")
        if inv == "none" and Level(alloc.uncertainty) in (Level.medium, Level.high):
            tier["investigate"] = investigate_tier_for(alloc.uncertainty)
            notes.append(f"{cid}: investigate tier set to {tier['investigate']!r} for {alloc.uncertainty} uncertainty")
        elif inv != "none" and inv not in ORDER:
            tier["investigate"] = defaults["investigate"]
            notes.append(f"{cid}: investigate tier {inv!r} unknown; set to {tier['investigate']!r}")
        alloc = alloc.model_copy(update={"tier": tier})
        budget = given_budget or budget_for(tier["build"], tiers)
        notes.extend(f"{cid}: {p}" for p in check_allocation(alloc))
        scope = [*c["paths"], *c.get("incidental", [])]
        for a in acceptance:
            if a.check.type == "test" and a.check.ref:
                file_part = a.check.ref.split("::")[0]
                if not any(matches(g, file_part) for g in scope):
                    notes.append(f"{cid}: acceptance {a.id} names {file_part}, outside the contract's paths; a BUILD worker could not write it")
        all_interfaces.append(interfaces)
        contracts.append(Contract(
            id=cid, plan=plan_id, title=c["title"], status=ContractStatus.draft,
            depends_on=list(dict.fromkeys(ids[d] for d in c.get("depends_on", []))), paths=list(c["paths"]), incidental=list(c.get("incidental", [])),
            acceptance=acceptance, interfaces=interfaces, checks=[known_checks[n] for n in c.get("checks", [])], allocation=alloc, budget=budget,
        ))

    notes.extend(_interface_notes(raw, ids, all_interfaces, repo_root, list(report.get("files_read") or [])))

    proposed = candidates_to_records(
        list(report.get("proposed_records") or []), list(report.get("unknowns") or []), records, repo_root, records_dir,
        cap_global=config.global_record_cap, by=by,
        declared_paths=[p for c in contracts for p in c.paths],
    )
    notes.extend(proposed.notes)
    graph = RecordGraph(records)
    bound_preview = {c.id: [r.id for r in select_records(c.paths, graph, repo_root)] for c in contracts}
    plan = Plan(
        id=plan_id, title=(report.get("title") or request)[:80], request=request, status=PlanStatus.plan_proposed,
        contracts=ids, proposed_records=[r.id for r in proposed.proposed], unknowns=list(report.get("unknowns") or []),
        depends_on_plans=depends_on_plans, files_read=list(report.get("files_read") or []),
        out_of_scope=list(report.get("out_of_scope") or []), verification_approach=report.get("verification_approach") or "",
        history=[PlanHistoryEntry(at=datetime.now(UTC), by=by, to=PlanStatus.plan_proposed)],
    )
    return PlanResult(plan=plan, contracts=contracts, proposed=proposed, problems=notes, bound_preview=bound_preview)


def render_draft(result: PlanResult) -> str:
    """The draft plan as YAML for the staff-engineer review: what the owner would see, and each contract in full."""
    p = result.plan
    data = {
        "plan": p.model_dump(mode="json", include={
            "id", "title", "request", "depends_on_plans", "unknowns", "files_read", "out_of_scope", "verification_approach"}),
        "proposed_records": [r.model_dump(mode="json", include={"id", "kind", "statement", "rationale", "binds"})
                             for r in result.proposed.proposed],
        "bound_records": {cid: list(rids) for cid, rids in result.bound_preview.items()},
        "corrections_and_warnings": list(result.problems),
        "contracts": [c.model_dump(mode="json") for c in result.contracts],
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def format_finding(f: ReviewFinding) -> str:
    return f"[{f.severity}] {f.contract}: {f.finding.strip().rstrip('.')}. Suggestion: {f.suggestion.strip()}"


def render_review(review: PlanReview) -> list[str]:
    head = f"Review: {review.verdict}"
    if review.verdict == "unavailable":
        head += " (the review session failed; this plan is unreviewed)"
    elif review.revised:
        head += " (the planner revised the plan once in response; findings refer to the first draft)"
    lines = [head]
    lines.extend(f"  - {format_finding(f)}" for f in review.findings)
    if review.notes.strip():
        lines.append(f"  notes: {review.notes.strip()}")
    return lines


def render_plan_review(result: PlanResult, tiers) -> str:
    p = result.plan
    lines = [f"Plan {p.id}: {p.title}", f"Request: {p.request}", ""]
    if result.covered:
        lines.append(f"{p.id} covered by {', '.join(p.covered_by)}; no contracts written")
        lines.append("")
        if p.unknowns:
            lines.append("Open questions:")
            lines.extend(f"  - {u}" for u in p.unknowns)
            lines.append("")
        if result.problems:
            lines.append("Planner's note:")
            lines.extend(f"  - {n}" for n in result.problems)
            lines.append("")
        return "\n".join(lines).rstrip()
    for c in result.contracts:
        a = c.allocation
        lines.append(f"{c.id}  {c.title}")
        lines.append("  paths: " + ", ".join(c.paths) + (f"  (incidental: {', '.join(c.incidental)})" if c.incidental else ""))
        lines.append("  depends on: " + (", ".join(c.depends_on) or "nothing"))
        for acc in c.acceptance:
            how = acc.check.type + (f" {acc.check.ref}" if acc.check.ref else "")
            lines.append(f"  - {acc.id}: {acc.statement}  [{how}]")
        if c.interfaces:
            lines.append("  interfaces:")
            lines += [f"    {itf.kind}: {itf.signature}" + (f"  ({itf.note})" if itf.note else "") for itf in c.interfaces]
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
    lines.append("Depends on plans: " + (", ".join(p.depends_on_plans) or "none"))
    shown = p.files_read[:20]
    lines.append(f"Files read ({len(p.files_read)}):" + (" " + ", ".join(shown) + (", ..." if len(p.files_read) > 20 else "") if shown else " none"))
    lines.append("Out of scope: " + ("; ".join(p.out_of_scope) or "none"))
    lines.append("Verification approach: " + (p.verification_approach or "none"))
    lines.append("")
    if result.proposed.proposed or result.proposed.dropped or result.proposed.skipped_rejected:
        lines.append("Records proposed by this plan:")
        lines.append(render_discovery_report(result.proposed).replace("\nNext: ateam records review   (approve as requirement or observation, reject with a reason, or skip)", ""))
        lines.append("")
    if p.unknowns:
        lines.append("Open questions:")
        lines.extend(f"  - {u}" for u in p.unknowns)
        lines.append("")
    if p.review is not None:
        lines.extend(render_review(p.review))
        lines.append("")
    if result.problems:
        lines.append("Notes (corrections applied or warnings):")
        lines.extend(f"  - {n}" for n in result.problems)
        lines.append("")
    first = result.contracts[0].id if result.contracts else "C-0001"
    lines.append(f"Approve: ateam approve {p.id} [--tier {first}=careful] [--tier {first}:test=careful] [--skip-records]")
    lines.append(f"Reject:  ateam reject {p.id} \"reason\"")
    return "\n".join(lines)
