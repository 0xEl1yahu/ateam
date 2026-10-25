"""A contract binds a worker only when it was frozen, has not changed since, and is runnable."""

from __future__ import annotations

from ateam.contracts.model import Contract, ContractStatus
from ateam.contracts.select import drift
from ateam.records.model import Record
from ateam.state.db import StateDB

RUNNABLE: frozenset[str] = frozenset({ContractStatus.ready, ContractStatus.building, ContractStatus.failed, ContractStatus.paused})


class ContractNotBinding(Exception):
    """Raised when work must not start under this contract."""


def require_binding(contract: Contract, records: dict[str, Record], db: StateDB) -> None:
    frozen_hash = db.contract_hash(contract.id)
    if frozen_hash is None:
        raise ContractNotBinding(f"{contract.id} is not frozen; run: ateam contracts freeze {contract.id}")
    if contract.status == ContractStatus.draft:
        raise ContractNotBinding(f"{contract.id} is a draft; freeze it first")
    if contract.status not in RUNNABLE:
        raise ContractNotBinding(f"{contract.id} is {contract.status}; only ready, building, failed or paused contracts run")
    stored = db.contract_status(contract.id)
    if stored not in RUNNABLE:
        raise ContractNotBinding(f"{contract.id} is {stored} in the state store; only ready, building, failed or paused contracts run")
    if stored != contract.status:
        raise ContractNotBinding(f"{contract.id} file says {contract.status} but the state store says {stored}; the file was edited after freeze")
    if contract.content_hash() != frozen_hash:
        raise ContractNotBinding(f"{contract.id} differs from its frozen hash (hand edit after freeze); re-freeze with --force if intended")
    drifted = drift(contract, records)
    if drifted:
        raise ContractNotBinding(f"{contract.id} has record drift: {', '.join(drifted)} changed, were superseded or are missing since the freeze")
