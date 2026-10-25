"""Handoff checkpoint: coordinator-verified facts plus a worker's unverified notes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from ateam.contracts.model import Contract
from ateam.gitops import GitRepo
from ateam.records.model import Record

HANDOFF_FILE = Path(".ateam-scratch") / "handoff.yaml"


@dataclass
class Checkpoint:
    base: str
    head: str
    commits: list[str]
    diff_names: list[str]
    dirty: list[tuple[str, str]]
    checks: list[dict]
    contract_hash: str
    record_hashes: dict[str, str]
    session_id: str | None
    notes: dict | None
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        data = dict(data)
        data["dirty"] = [tuple(x) for x in data.get("dirty", [])]
        return cls(**data)


def load_handoff(worktree: Path) -> tuple[dict | None, list[str]]:
    """The worker's handoff notes, or None with a warning when absent, empty, invalid or not a mapping."""
    path = worktree / HANDOFF_FILE
    if not path.is_file():
        return None, [f"no handoff file at {HANDOFF_FILE}"]
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return None, [f"handoff file is not valid YAML: {e}"]
    if data is None:
        return None, ["handoff file is empty"]
    if not isinstance(data, dict):
        return None, ["handoff file is not a mapping"]
    return data, []


def collect(
    git: GitRepo, worktree: Path, base: str, contract: Contract, records: dict[str, Record],
    session_id: str | None, checks: list[dict],
) -> Checkpoint:
    head = git.head()
    log = git.run("log", "--format=%h %s", f"{base}..{head}") if head != base else ""
    notes, warnings = load_handoff(worktree)
    return Checkpoint(
        base=base,
        head=head,
        commits=[line for line in log.splitlines() if line],
        diff_names=git.diff_names(base),
        dirty=git.status_porcelain(),
        checks=checks,
        contract_hash=contract.content_hash(),
        record_hashes={ref.id: records[ref.id].content_hash() for ref in contract.records if ref.id in records}
        or {rid: r.content_hash() for rid, r in records.items()},
        session_id=session_id,
        notes=notes,
        warnings=warnings,
    )
