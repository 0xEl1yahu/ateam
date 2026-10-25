"""In-memory graph over records: binding by path, supersession, integrity queries."""

from __future__ import annotations

from ateam.globs import matches
from ateam.records.model import Record, Status


class RecordGraph:
    def __init__(self, records: dict[str, Record]) -> None:
        self.records = records

    def _pool(self, approved_only: bool) -> list[Record]:
        pool = [r for r in self.records.values() if not approved_only or r.status == Status.approved]
        return sorted(pool, key=lambda r: r.id)

    def binding(self, path: str, approved_only: bool = True) -> list[Record]:
        """Records whose globs match `path`, sorted by id."""
        return [r for r in self._pool(approved_only) if any(matches(g, path) for g in r.binds.paths)]

    def globals(self, approved_only: bool = True) -> list[Record]:
        return [r for r in self._pool(approved_only) if r.is_global()]

    def chain(self, record_id: str) -> list[str]:
        """Ids reachable through `supersedes` from `record_id`, depth-first, excluding the start."""
        seen: list[str] = []
        stack = list(self.records[record_id].supersedes) if record_id in self.records else []
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in self.records:
                continue
            seen.append(cur)
            stack.extend(self.records[cur].supersedes)
        return seen

    def superseders(self, record_id: str) -> list[str]:
        return sorted(r.id for r in self.records.values() if record_id in r.supersedes)

    def dangling(self) -> list[tuple[str, str]]:
        return sorted((r.id, t) for r in self.records.values() for t in r.supersedes if t not in self.records)

    def cycles(self) -> list[list[str]]:
        cycles: list[list[str]] = []
        for start in sorted(self.records):
            if start in self.chain(start):
                cycle = [start, *[c for c in self.chain(start) if c != start]]
                if not any(set(cycle) == set(c) for c in cycles):
                    cycles.append(cycle)
        return cycles
