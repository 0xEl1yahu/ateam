"""Turning complexity, risk and uncertainty into tiers, and checking a planner's allocation."""

from __future__ import annotations

from importlib import resources

from ateam.contracts.model import Allocation, Level
from ateam.tiers import ORDER, at_least

_COMPLEXITY = {Level.low: "light", Level.medium: "standard", Level.high: "careful"}
_INVESTIGATE = {Level.low: "none", Level.medium: "light", Level.high: "standard"}
_RISK_FLOOR = {Level.low: "light", Level.medium: "standard", Level.high: "careful"}


def tier_for_complexity(level: Level) -> str:
    return _COMPLEXITY[Level(level)]


def investigate_tier_for(uncertainty: Level) -> str:
    return _INVESTIGATE[Level(uncertainty)]


def tier_for_test(risk: Level, build_tier: str) -> str:
    """Risk sets a floor for the TEST tier; TEST never runs below the BUILD it checks."""
    floor = _RISK_FLOOR[Level(risk)]
    return "careful" if Level(risk) == Level.high else at_least(build_tier, floor)


def default_tiers(a: Allocation) -> dict[str, str]:
    build = tier_for_complexity(a.complexity)
    return {"build": build, "test": tier_for_test(a.risk, build), "investigate": investigate_tier_for(a.uncertainty)}


def check_allocation(a: Allocation) -> list[str]:
    """Human-readable problems with a planner's allocation. Empty when it is acceptable."""
    problems: list[str] = []
    for role in ("build", "test", "investigate"):
        name = a.tier.get(role)
        if name is None:
            problems.append(f"{role}: no tier given")
        elif name not in ORDER and not (role == "investigate" and name == "none"):
            problems.append(f"{role}: unknown tier {name!r}; known: {', '.join(ORDER)}")
    build, test = a.tier.get("build"), a.tier.get("test")
    if build in ORDER and test in ORDER and ORDER.index(test) < ORDER.index(build):
        problems.append(f"test tier {test!r} is below build tier {build!r}; TEST never runs below BUILD, use {build!r}")
    if Level(a.uncertainty) in (Level.medium, Level.high) and a.tier.get("investigate", "none") == "none":
        problems.append(f"investigate tier is 'none' with {a.uncertainty} uncertainty; the rubric asks for {investigate_tier_for(a.uncertainty)!r}")
    if not a.reasoning.strip():
        problems.append("reasoning is empty; the planner must cite the signals it used")
    return problems


def rubric_text() -> str:
    return resources.files("ateam").joinpath("data/rubric.md").read_text()
