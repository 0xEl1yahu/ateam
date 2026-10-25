"""Contracts on disk: one YAML file per contract under .ateam/contracts/."""

from __future__ import annotations

from pathlib import Path

import yaml

from ateam.contracts.model import Contract
from ateam.records.store import _FILE_RE


class ContractStoreError(Exception):
    pass


def load_contracts(contracts_dir: Path) -> dict[str, Contract]:
    contracts: dict[str, Contract] = {}
    sources: dict[str, Path] = {}
    for path in sorted(contracts_dir.glob("*.y*ml")):
        m = _FILE_RE.match(path.name)
        contract = Contract.model_validate(yaml.safe_load(path.read_text()) or {})
        if not m or m.group("id") != contract.id:
            raise ContractStoreError(f"{path.name}: filename does not match contract id {contract.id!r}")
        if contract.id in contracts:
            raise ContractStoreError(f"duplicate contract id {contract.id}: {sources[contract.id].name} and {path.name}")
        contracts[contract.id] = contract
        sources[contract.id] = path
    return contracts


def save_contract(contracts_dir: Path, contract: Contract) -> Path:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    path = contracts_dir / f"{contract.id}-{contract.slug()}.yaml"
    for old in contracts_dir.glob(f"{contract.id}-*.yaml"):
        if old != path:
            old.unlink()
    path.write_text(yaml.safe_dump(contract.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
    return path


def next_id(contracts_dir: Path, prefix: str = "C") -> str:
    from ateam.records.store import next_id as _next

    return _next(contracts_dir, prefix)
