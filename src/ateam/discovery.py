"""Discovery: turning a worker's candidate records into proposed records the owner can review.

The worker proposes; the coordinator decides. Classification is corrected here (code-only evidence
is observation; "documented" without a document is inferred), dead globs are dropped, rejected
fingerprints are never re-proposed, and global records are capped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ateam.globs import expand, is_valid_pattern, matches
from ateam.records.model import (
    DOCUMENTED_EVIDENCE, Basis, Binds, Evidence, HistoryEntry, Origin, Record, Status, compute_fingerprint,
)
from ateam.records.store import next_id

_CODE_ONLY = {"code", "test", "commit"}


def _normalise_statement(statement: str) -> str:
    return " ".join(statement.casefold().split())


@dataclass
class DiscoveryResult:
    proposed: list[Record] = field(default_factory=list)
    dropped: list[tuple[dict, str]] = field(default_factory=list)
    skipped_rejected: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    capped: int = 0


def candidates_to_records(
    candidates: list[dict], unknowns: list[str], existing: dict[str, Record], repo_root: Path, records_dir: Path,
    cap_global: int, by: str = "ateam-init", declared_paths: list[str] | tuple[str, ...] = ()
) -> DiscoveryResult:
    result = DiscoveryResult(unknowns=list(unknowns))
    known: dict[str, list[Record]] = {}
    for r in existing.values():
        known.setdefault(r.fingerprint, []).append(r)
    seen_batch: set[tuple[str, str]] = set()
    counter = int(next_id(records_dir).split("-")[1])
    globals_kept = 0
    now = datetime.now(UTC)

    for cand in candidates:
        if not str(cand.get("statement") or "").strip():
            result.dropped.append((cand, "empty statement"))
            continue
        try:
            evidence = [Evidence.model_validate(e) for e in cand.get("evidence", [])]
            origin = Origin(cand.get("origin", "inferred"))
            basis = Basis(cand.get("basis", "observation"))
        except (ValidationError, ValueError) as e:
            result.dropped.append((cand, f"invalid origin, basis or evidence: {str(e).splitlines()[0]}"))
            continue
        if not evidence:
            result.dropped.append((cand, "no evidence cited; a record needs at least one reference"))
            continue
        fingerprint = compute_fingerprint(evidence)
        statement_key = _normalise_statement(cand["statement"])
        same_evidence = known.get(fingerprint, [])
        rejected = next((r for r in same_evidence if r.status == Status.rejected), None)
        if rejected is not None:
            result.skipped_rejected.append(rejected.id)
            continue
        if any(_normalise_statement(r.statement) == statement_key for r in same_evidence):
            continue
        if (fingerprint, statement_key) in seen_batch:
            result.dropped.append((cand, "duplicate of an earlier candidate in this batch (same evidence and statement)"))
            continue

        paths = list(cand.get("paths") or [])
        dead = [g for g in paths if not is_valid_pattern(g) or not expand([g], repo_root)]
        pending = [g for g in dead if is_valid_pattern(g) and any(g == d or matches(g, d) for d in declared_paths)]
        if len(pending) < len(dead):
            gone = next(g for g in dead if g not in pending)
            result.dropped.append((cand, f"dead glob {gone!r}: matches no file in the target"))
            continue

        rationale = cand.get("rationale") or None
        if pending:
            rationale = f"{rationale or ''} (pending: path declared by this plan)".strip()
            result.notes.append(f"pending: kept a record bound to {', '.join(pending)}: the path does not exist yet but a contract in this plan declares it")
        if origin == Origin.documented and not any(e.type in DOCUMENTED_EVIDENCE for e in evidence):
            origin = Origin.inferred
            rationale = f"{rationale or ''} (downgraded: no documentary evidence)".strip()
        if all(e.type in _CODE_ONLY for e in evidence):
            basis = Basis.observation

        if not paths:
            if globals_kept >= cap_global:
                result.dropped.append((cand, f"global cap of {cap_global} reached"))
                result.capped += 1
                continue
            globals_kept += 1

        try:
            record = Record(
                id=f"R-{counter:04d}", kind=cand["kind"], status=Status.proposed, origin=origin, basis=basis,
                statement=cand["statement"].strip(), rationale=rationale, evidence=evidence, binds=Binds(paths=paths),
                serves=[s.strip() for s in cand.get("serves", []) if s.strip()], fingerprint=fingerprint,
                history=[HistoryEntry(at=now, by=by, to=Status.proposed, why="discovery")],
            )
        except (ValidationError, KeyError) as e:
            result.dropped.append((cand, f"invalid candidate: {str(e).splitlines()[0]}"))
            continue
        counter += 1
        seen_batch.add((fingerprint, statement_key))
        result.proposed.append(record)
    return result


def render_discovery_report(result: DiscoveryResult) -> str:
    lines: list[str] = [f"{len(result.proposed)} records proposed"]
    for kind in ("decision", "invariant", "convention"):
        group = [r for r in result.proposed if r.kind == kind]
        if not group:
            continue
        lines.append(f"\n{kind.capitalize()}s:")
        for r in group:
            lines.append(f"  {r.id}  [{r.origin}, {r.basis}]  {r.statement}")
            lines.append("        evidence: " + "; ".join(f"{e.type} {e.ref}" for e in r.evidence))
            lines.append("        binds: " + (", ".join(r.binds.paths) if r.binds.paths else "(global)"))
    if result.dropped:
        lines.append("\nDropped:")
        lines.extend(f"  {c.get('statement', '?')[:70]}  ->  {why}" for c, why in result.dropped)
    if result.skipped_rejected:
        lines.append("\nSkipped as previously rejected: " + ", ".join(result.skipped_rejected))
    if result.unknowns:
        lines.append("\nUnknowns (not stored; answer them and they can become records):")
        lines.extend(f"  - {u}" for u in result.unknowns)
    if result.proposed:
        lines.append("\nNext: ateam records review   (approve as requirement or observation, reject with a reason, or skip)")
    return "\n".join(lines)
