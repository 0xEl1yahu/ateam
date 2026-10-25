import pytest

from ateam.allocation import (
    check_allocation, default_tiers, investigate_tier_for, rubric_text, tier_for_test, tier_for_complexity,
)
from ateam.contracts.model import Allocation, Level


@pytest.mark.parametrize("level,tier", [(Level.low, "light"), (Level.medium, "standard"), (Level.high, "careful")])
def test_complexity_mapping(level, tier):
    assert tier_for_complexity(level) == tier


@pytest.mark.parametrize("level,tier", [(Level.low, "none"), (Level.medium, "light"), (Level.high, "standard")])
def test_investigate_mapping(level, tier):
    assert investigate_tier_for(level) == tier


@pytest.mark.parametrize("risk,build,expected", [
    (Level.low, "light", "light"), (Level.low, "careful", "careful"),
    (Level.medium, "light", "standard"), (Level.medium, "careful", "careful"),
    (Level.high, "light", "careful"), (Level.high, "standard", "careful"),
])
def test_test_tier_mapping(risk, build, expected):
    assert tier_for_test(risk, build) == expected


def test_default_tiers_mapping():
    a = Allocation(complexity=Level.medium, risk=Level.high, uncertainty=Level.low, reasoning="x", tier={})
    assert default_tiers(a) == {"build": "standard", "test": "careful", "investigate": "none"}


def test_check_allocation_flags_test_below_build():
    a = Allocation(complexity=Level.high, risk=Level.low, uncertainty=Level.low, reasoning="x",
                   tier={"build": "careful", "test": "light", "investigate": "none"})
    problems = check_allocation(a)
    assert any("test" in p and "careful" in p for p in problems)


def test_check_allocation_flags_none_investigate_on_high_uncertainty_and_unknown_tier_and_empty_reasoning():
    a = Allocation(complexity=Level.low, risk=Level.low, uncertainty=Level.high, reasoning="",
                   tier={"build": "light", "test": "light", "investigate": "none"})
    problems = check_allocation(a)
    assert any("investigate" in p for p in problems) and any("reasoning" in p for p in problems)
    b = a.model_copy(update={"tier": {"build": "huge", "test": "light", "investigate": "light"}, "reasoning": "ok"})
    assert any("unknown tier" in p and "huge" in p for p in check_allocation(b))
    good = Allocation(complexity=Level.low, risk=Level.low, uncertainty=Level.low, reasoning="ok",
                      tier={"build": "light", "test": "light", "investigate": "none"})
    assert check_allocation(good) == []


def test_rubric_text_mentions_three_dimensions_and_no_file_count():
    text = rubric_text()
    for word in ("Complexity", "Risk", "Uncertainty", "light", "standard", "careful"):
        assert word in text
    assert "file count" in text.lower() and "not a signal" in text.lower()


@pytest.mark.parametrize("tier,attempts", [("light", 2), ("standard", 3), ("careful", 3)])
def test_budget_for_each_tier(tier, attempts):
    from ateam.allocation import budget_for
    from ateam.roles import Role
    from ateam.tiers import load_tiers

    tiers = load_tiers()
    b = budget_for(tier, tiers)
    assert b.attempts == attempts
    assert b.usd_per_attempt == tiers[tier][Role.build].usd and b.max_turns == tiers[tier][Role.build].max_turns
