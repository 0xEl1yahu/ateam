"""Validation of a record set against its own rules and the target repo."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ateam.globs import expand, is_valid_pattern
from ateam.records.graph import RecordGraph
from ateam.records.model import DOCUMENTED_EVIDENCE, Origin, Record, Status


@dataclass(frozen=True)
class Problem:
    level: Literal["error", "warning"]
    record_id: str | None
    message: str


def validate_records(
    records: dict[str, Record], repo_root: Path, known_hashes: dict[str, str] | None = None
) -> list[Problem]:
    problems: list[Problem] = []
    err = lambda rid, msg: problems.append(Problem("error", rid, msg))  # noqa: E731
    warn = lambda rid, msg: problems.append(Problem("warning", rid, msg))  # noqa: E731
    graph = RecordGraph(records)

    for rid, r in sorted(records.items()):
        for g in [*r.binds.paths, *r.binds.incidental]:
            if not is_valid_pattern(g):
                err(rid, f"invalid glob {g!r}: must be relative, no './', no trailing '/', no '..'")
        if r.origin == Origin.documented and not any(e.type in DOCUMENTED_EVIDENCE for e in r.evidence):
            err(rid, "origin is documented but no evidence of type doc, claude-md, agents-md or commit-message")

    for from_id, missing in graph.dangling():
        err(from_id, f"supersedes {missing}, which does not exist")
    for cycle in graph.cycles():
        err(cycle[0], f"supersession cycle: {' -> '.join(cycle)}")

    for rid, r in sorted(records.items()):
        if r.status == Status.approved:
            for target in r.supersedes:
                t = records.get(target)
                if t and t.status in (Status.approved, Status.proposed):
                    err(rid, f"supersession incomplete: {target} is still {t.status}")
        if r.status == Status.superseded and not graph.superseders(rid):
            err(rid, "superseded but has no superseder")

    for rid, r in sorted(records.items()):
        for g in r.binds.paths:
            if is_valid_pattern(g) and not expand([g], repo_root):
                warn(rid, f"dead glob {g!r}: matches no file")

    by_phrase: dict[str, list[str]] = defaultdict(list)
    for rid, r in sorted(records.items()):
        for phrase in r.serves:
            by_phrase[phrase.strip().lower()].append(rid)
    for phrase, ids in sorted(by_phrase.items()):
        if len(ids) > 1:
            warn(None, f"serves phrase {phrase!r} appears in {', '.join(ids)}")

    if known_hashes:
        for rid, r in sorted(records.items()):
            if rid in known_hashes and known_hashes[rid] != r.content_hash():
                warn(rid, "hand edit: content differs from the hash recorded at last approval")

    return problems
