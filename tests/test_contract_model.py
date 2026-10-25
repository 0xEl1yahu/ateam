from ateam.contracts.model import Budget, ContractStatus


def test_defaults(sample_contract):
    assert sample_contract.status == ContractStatus.draft
    assert sample_contract.budget == Budget(attempts=3, usd_per_attempt=2.0, max_turns=60)
    assert sample_contract.records == [] and sample_contract.depends_on == []


def test_content_hash_ignores_status(sample_contract):
    h = sample_contract.content_hash()
    assert sample_contract.model_copy(update={"status": ContractStatus.building}).content_hash() == h
    assert sample_contract.model_copy(update={"title": "x"}).content_hash() != h


def test_allocation_tier_keys_and_slug(sample_contract):
    assert set(sample_contract.allocation.tier) == {"build", "test", "investigate"}
    assert sample_contract.slug() == "expose-reading-state-through-the-books"
