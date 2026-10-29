import pytest

from ateam.roles import Role
from ateam.tiers import TierSpec, at_least, attempt_cost, load_tiers, resolve, spend_total


def test_defaults():
    t = load_tiers()
    assert set(t) == {"light", "standard", "careful"}
    assert t["light"][Role.build] == TierSpec(model="haiku", effort="low", max_turns=40, usd=1.0)
    assert t["careful"][Role.test].model == "opus" and t["careful"][Role.test].max_turns == 80


def test_override_deep_merges():
    t = load_tiers({"standard": {"build": {"model": "opus"}}})
    assert t["standard"][Role.build].model == "opus" and t["standard"][Role.build].max_turns == 60
    assert t["standard"][Role.test].model == "sonnet"


def test_resolve(sample_contract):
    t = load_tiers()
    assert resolve(sample_contract, Role.build, t).model == "sonnet"
    assert resolve(sample_contract, Role.test, t).model == "opus"
    with pytest.raises(ValueError):
        resolve(sample_contract, Role.investigate, t)


def test_at_least():
    assert at_least("standard", "light") == "standard" and at_least("light", "careful") == "careful" and at_least("standard", "standard") == "standard"


def _row(**fields):
    return {"attempt_id": 1, "role": "build", "tier": "standard", "cost_usd": None, **fields}


def test_attempt_cost_unknown_counts_cap():
    t = load_tiers()
    assert attempt_cost(_row(cost_usd=0.0), t) == (0.0, False)
    assert attempt_cost(_row(cost_usd=0.42), t) == (0.42, False)
    assert attempt_cost(_row(tier="light", cost_usd=None), t) == (t["light"][Role.build].usd, True)


def test_spend_total_counts_capped_sessions():
    t = load_tiers()
    rows = [_row(cost_usd=0.5), _row(cost_usd=None, tier="light"), _row(cost_usd=0.0)]
    assert spend_total(rows, t) == (0.5 + t["light"][Role.build].usd, 1)
    assert spend_total([], t) == (0.0, 0)


def test_attempt_cost_unknown_tier_counts_highest_cap():
    t = load_tiers()
    highest = max(spec.usd for tier in t.values() for role, spec in tier.items() if role == Role.build)
    assert attempt_cost(_row(tier="nope", cost_usd=None), t) == (highest, True)
    assert highest > 0
    any_role = max(spec.usd for tier in t.values() for spec in tier.values())
    assert attempt_cost(_row(role="no_such_role", tier="nope", cost_usd=None), t) == (any_role, True)
