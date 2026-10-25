"""Pure state transitions for records. Each returns a new Record with history appended."""

from __future__ import annotations

from datetime import UTC, datetime

from ateam.records.model import Basis, HistoryEntry, Record, Status


class LifecycleError(Exception):
    pass


def _moved(record: Record, to: Status, by: str, why: str | None, at: datetime | None, **extra) -> Record:
    entry = HistoryEntry(at=at or datetime.now(UTC), by=by, to=to, why=why)
    return record.model_copy(update={"status": to, "history": [*record.history, entry], **extra})


def _require(record: Record, *allowed: Status) -> None:
    if record.status not in allowed:
        raise LifecycleError(f"{record.id} is {record.status}; expected {' or '.join(allowed)}")


def approve(record: Record, by: str = "owner", basis: Basis | None = None, at: datetime | None = None) -> Record:
    _require(record, Status.proposed)
    extra = {"basis": basis} if basis is not None else {}
    return _moved(record, Status.approved, by, None, at, **extra)


def reject(record: Record, reason: str, by: str = "owner", at: datetime | None = None) -> Record:
    _require(record, Status.proposed)
    return _moved(record, Status.rejected, by, reason, at)


def supersede(old: Record, new: Record, by: str = "owner", at: datetime | None = None) -> tuple[Record, Record]:
    _require(old, Status.approved)
    _require(new, Status.proposed)
    supersedes = new.supersedes if old.id in new.supersedes else [*new.supersedes, old.id]
    new2 = _moved(new, Status.approved, by, f"supersedes {old.id}", at, supersedes=supersedes)
    old2 = _moved(old, Status.superseded, by, f"superseded by {new.id}", at)
    return old2, new2


def retire(record: Record, reason: str, by: str = "owner", at: datetime | None = None) -> Record:
    _require(record, Status.approved)
    return _moved(record, Status.retired, by, reason, at)
