import pytest

from ateam.roles import Role
from ateam.tiers import TierSpec, at_least, load_tiers, resolve


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
