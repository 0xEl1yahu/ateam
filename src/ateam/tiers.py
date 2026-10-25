"""Named allocation tiers mapped to model, effort, turn cap and budget per role."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from ateam.contracts.model import Contract
from ateam.roles import Role

ORDER = ["light", "standard", "careful"]


@dataclass(frozen=True)
class TierSpec:
    model: str
    effort: str
    max_turns: int
    usd: float


def _defaults() -> dict[str, Any]:
    text = resources.files("ateam").joinpath("data/tiers.yaml").read_text()
    return yaml.safe_load(text)


def load_tiers(overrides: dict | None = None) -> dict[str, dict[Role, TierSpec]]:
    raw = _defaults()
    for tier, roles in (overrides or {}).items():
        raw.setdefault(tier, {})
        for role, fields in (roles or {}).items():
            raw[tier].setdefault(role, {}).update(fields or {})
    return {
        tier: {Role(role): TierSpec(**fields) for role, fields in roles.items()}
        for tier, roles in raw.items()
    }


def resolve(contract: Contract, role: Role, tiers: dict[str, dict[Role, TierSpec]]) -> TierSpec:
    name = contract.allocation.tier.get(role.value, "none")
    if name == "none":
        raise ValueError(f"{contract.id} allocates no {role.value} tier")
    if name not in tiers:
        raise ValueError(f"{contract.id} names unknown tier {name!r} for {role.value}")
    return tiers[name][role]


def at_least(build_tier: str, test_tier: str) -> str:
    """The higher of two tier names, so TEST never runs below the BUILD it checks."""
    return max(build_tier, test_tier, key=ORDER.index)
