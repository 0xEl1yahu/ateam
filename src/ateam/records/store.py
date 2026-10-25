"""Records on disk: one YAML file per record under .ateam/records/."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from ateam.records.model import Record

_FILE_RE = re.compile(r"^(?P<id>[A-Z]-\d{4})(?:-.*)?\.ya?ml$")


class RecordStoreError(Exception):
    pass


def load_records(records_dir: Path) -> dict[str, Record]:
    records: dict[str, Record] = {}
    sources: dict[str, Path] = {}
    for path in sorted(records_dir.glob("*.y*ml")):
        m = _FILE_RE.match(path.name)
        data = yaml.safe_load(path.read_text()) or {}
        record = Record.model_validate(data)
        if not m or m.group("id") != record.id:
            raise RecordStoreError(f"{path.name}: filename does not match record id {record.id!r}")
        if record.id in records:
            raise RecordStoreError(f"duplicate record id {record.id}: {sources[record.id].name} and {path.name}")
        records[record.id] = record
        sources[record.id] = path
    return records


def save_record(records_dir: Path, record: Record) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{record.id}-{record.slug()}.yaml"
    for old in records_dir.glob(f"{record.id}-*.yaml"):
        if old != path:
            old.unlink()
    path.write_text(yaml.safe_dump(record.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
    return path


def next_id(records_dir: Path, prefix: str = "R") -> str:
    highest = 0
    if records_dir.is_dir():
        for path in records_dir.glob(f"{prefix}-*.y*ml"):
            m = _FILE_RE.match(path.name)
            if m:
                highest = max(highest, int(m.group("id").split("-")[1]))
    return f"{prefix}-{highest + 1:04d}"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
