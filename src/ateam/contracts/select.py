"""Which records bind a contract, and freezing that selection into the contract."""

from __future__ import annotations

from pathlib import Path

from ateam.contracts.model import Contract, ContractStatus, RecordRef
from ateam.globs import expand, matches
from ateam.records.graph import RecordGraph
from ateam.records.model import Record, Status


def _prefix(glob_pattern: str) -> str:
    return glob_pattern[:-3] if glob_pattern.endswith("/**") else glob_pattern


def _globs_overlap(record_glob: str, contract_glob: str) -> bool:
    if record_glob == contract_glob:
        return True
    r, c = _prefix(record_glob), _prefix(contract_glob)
    return r == c or c.startswith(r + "/") or r.startswith(c + "/") or matches(record_glob, c)


def select_records(paths: list[str], graph: RecordGraph, repo_root: Path) -> list[Record]:
    """Approved records that are global, bind an existing file under `paths`, or whose glob overlaps a contract glob."""
    chosen: dict[str, Record] = {r.id: r for r in graph.globals()}
    for file in expand(paths, repo_root):
        for r in graph.binding(file):
            chosen[r.id] = r
    for r in graph.records.values():
        if r.status != Status.approved or r.is_global():
            continue
        if any(_globs_overlap(rg, cg) for rg in r.binds.paths for cg in paths):
            chosen[r.id] = r
    return [chosen[k] for k in sorted(chosen)]


def freeze(contract: Contract, records: list[Record]) -> Contract:
    refs = [RecordRef(id=r.id, hash=r.content_hash()) for r in sorted(records, key=lambda r: r.id)]
    status = ContractStatus.ready if not contract.depends_on else ContractStatus.draft
    return contract.model_copy(update={"records": refs, "status": status})


def drift(contract: Contract, records: dict[str, Record]) -> list[str]:
    """Frozen record ids whose current content differs, are missing, or are no longer approved."""
    drifted: list[str] = []
    for ref in contract.records:
        current = records.get(ref.id)
        if current is None or current.status != Status.approved or current.content_hash() != ref.hash:
            drifted.append(ref.id)
    return drifted
