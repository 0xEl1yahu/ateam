"""The owner's instructions for a contract (pause and resume design, owner 2026-10-26).

Guidance within the frozen contract, never a change to it: one YAML file per contract in the intent repo,
`guidance/<contract-id>.yaml`. Each entry records the contract hash it was written against; after a re-freeze
changes the hash, the entry stays for history and is no longer delivered. Only BUILD briefs carry live entries.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel


class GuidanceEntry(BaseModel):
    at: datetime
    source: str            # "owner", or "A-<attempt_id>" when adopted from a pause investigation
    contract_hash: str
    text: str


def _file(directory: Path, contract_id: str) -> Path:
    return directory / f"{contract_id}.yaml"


def load_guidance(directory: Path, contract_id: str) -> list[GuidanceEntry]:
    path = _file(directory, contract_id)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [GuidanceEntry.model_validate(e) for e in data.get("entries") or []]


def append_guidance(directory: Path, contract_id: str, entry: GuidanceEntry) -> Path:
    entries = [*load_guidance(directory, contract_id), entry]
    directory.mkdir(parents=True, exist_ok=True)
    path = _file(directory, contract_id)
    path.write_text(yaml.safe_dump({"contract": contract_id, "entries": [e.model_dump(mode="json") for e in entries]},
                                   sort_keys=False, allow_unicode=True))
    return path


def live_guidance(entries: list[GuidanceEntry], contract_hash: str) -> list[GuidanceEntry]:
    return [e for e in entries if e.contract_hash == contract_hash]
