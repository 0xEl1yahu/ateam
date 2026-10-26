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


def test_interfaces_round_trip_and_unknown_kind_rejected(sample_contract):
    import pytest
    from pydantic import ValidationError

    from ateam.contracts.model import Contract, Interface

    ifs = [Interface(kind="produces", name="integrate", signature="integrate(plan_id: str) -> MergeProposal"),
           Interface(kind="consumes", name="load_plan", signature="load_plan(id: str) -> Plan", note="existing")]
    c = sample_contract.model_copy(update={"interfaces": ifs})
    assert Contract.model_validate_json(c.model_dump_json()).interfaces == ifs
    with pytest.raises(ValidationError):
        Interface(kind="exports", name="x", signature="x()")


def test_interfaces_default_empty_and_bound_by_hash(sample_contract):
    from ateam.contracts.model import Interface

    assert sample_contract.interfaces == []
    h = sample_contract.content_hash()
    c = sample_contract.model_copy(update={"interfaces": [Interface(kind="produces", name="f", signature="f()")]})
    assert c.content_hash() != h
    # an empty list leaves the hash as it was before the field existed, so old frozen contracts stay frozen
    import hashlib, json
    data = json.loads(sample_contract.model_dump_json(exclude={"status", "interfaces"}))
    assert h == hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
