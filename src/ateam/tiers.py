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


def _unknown_cap(role: str, tiers: dict[str, dict[Role, TierSpec]]) -> float:
    """Highest usd cap any tier gives this role; if no tier defines the role, the highest of any role."""
    specs = [spec for roles in tiers.values() for r, spec in roles.items() if r.value == role]
    if not specs:
        specs = [spec for roles in tiers.values() for spec in roles.values()]
    return max((spec.usd for spec in specs), default=0.0)


def attempt_cost(attempt: dict[str, Any], tiers: dict[str, dict[Role, TierSpec]]) -> tuple[float, bool]:
    """The usd an attempt counts as. An unknown cost (NULL) counts at the session's tier cap.

    Returns (amount, capped); capped is True when the cost was unknown and a cap was used.
    """
    cost = attempt.get("cost_usd")
    if cost is not None:
        return float(cost), False
    role = str(attempt.get("role"))
    spec = tiers.get(str(attempt.get("tier")), {}).get(role)
    if spec is None:
        return _unknown_cap(role, tiers), True
    return spec.usd, True


def spend_total(attempts: list[dict[str, Any]], tiers: dict[str, dict[Role, TierSpec]]) -> tuple[float, int]:
    """Total usd over attempts, and how many were counted at a cap rather than a known cost."""
    total, capped = 0.0, 0
    for attempt in attempts:
        amount, was_capped = attempt_cost(attempt, tiers)
        total += amount
        capped += was_capped
    return total, capped
