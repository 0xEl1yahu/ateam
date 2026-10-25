"""Record transitions with guards and a git commit of only .ateam/ paths."""

from __future__ import annotations

from ateam.config import TargetConfig
from ateam.intent import IntentError, commit_intent, intent_git
from ateam.paths import TargetPaths
from ateam.records import lifecycle
from ateam.records.lifecycle import LifecycleError
from ateam.records.model import Basis, Record
from ateam.records.store import load_records, save_record
from ateam.records.validate import validate_records


class RecordService:
    """Record transitions, committed in the target's private intent repo (never in the target)."""

    def __init__(self, paths: TargetPaths, config: TargetConfig) -> None:
        self.paths = paths
        self.config = config

    def _guard(self) -> None:
        try:
            git = intent_git(self.paths)
        except IntentError as e:
            raise LifecycleError(str(e)) from e
        if not git.is_clean():
            raise LifecycleError(f"intent repo {self.paths.intent} has uncommitted changes; commit or discard them first")

    def _load(self, *ids: str) -> list[Record]:
        records = load_records(self.paths.records)
        missing = [i for i in ids if i not in records]
        if missing:
            raise LifecycleError(f"unknown record id(s): {', '.join(missing)}")
        return [records[i] for i in ids]

    def _check(self, *changed: Record) -> None:
        """Refuse a transition that would commit a record set with validation errors on the changed ids."""
        future = {**load_records(self.paths.records), **{r.id: r for r in changed}}
        ids = {r.id for r in changed}
        errors = [p for p in validate_records(future, self.paths.root) if p.level == "error" and (p.record_id in ids or p.record_id is None)]
        if errors:
            raise LifecycleError("; ".join(f"{p.record_id or '-'}: {p.message}" for p in errors))

    def _commit(self, message: str, *records: Record) -> None:
        self._check(*records)
        for r in records:
            save_record(self.paths.records, r)
        commit_intent(self.paths, message, ["records"])

    def approve(self, record_id: str, basis: Basis | None = None) -> Record:
        self._guard()
        (record,) = self._load(record_id)
        new = lifecycle.approve(record, basis=basis)
        self._commit(f"ateam: approve {record_id}", new)
        return new

    def reject(self, record_id: str, reason: str) -> Record:
        self._guard()
        (record,) = self._load(record_id)
        new = lifecycle.reject(record, reason)
        self._commit(f"ateam: reject {record_id}", new)
        return new

    def supersede(self, old_id: str, new_id: str) -> tuple[Record, Record]:
        self._guard()
        old, new = self._load(old_id, new_id)
        old2, new2 = lifecycle.supersede(old, new)
        self._commit(f"ateam: supersede {old_id} with {new_id}", old2, new2)
        return old2, new2

    def retire(self, record_id: str, reason: str) -> Record:
        self._guard()
        (record,) = self._load(record_id)
        new = lifecycle.retire(record, reason)
        self._commit(f"ateam: retire {record_id}", new)
        return new
