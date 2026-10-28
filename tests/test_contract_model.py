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


def test_normalise_header_forms():
    import pytest

    from ateam.contracts.model import normalise_header

    headers = [
        "def name(a: int, b: str = 'x:)') -> str | None:",
        "async def name(a: dict[str, int], *, c: int = 1) -> list[str]:",
        "class Name(Base):",
        "class Name:",
        "def first[T](x: T) -> T:",
        "def f(a: 'Plan') -> 'Plan':",
    ]
    for h in headers:
        assert normalise_header(h) == h
    assert normalise_header("def  name( a: int,  b: int )   ->  str:") == "def name(a: int, b: int) -> str:"
    assert normalise_header("def name(\n        a: int,\n    b: int,\n) -> str | None:") == normalise_header(
        "def name(a: int, b: int,) -> str | None:"
    )
    for not_header in ["name(a, b)", "--verbose", '{"a": 1}', "def name(a)", "class", "run the thing:", ""]:
        assert normalise_header(not_header) is None


def test_interface_code_header():
    from ateam.contracts.model import Interface

    assert Interface(kind="produces", name="f", signature="def f(a: int) -> int:").code_header() == "def f(a: int) -> int:"
    assert Interface(kind="produces", name="f", signature="f(a, b)").code_header() is None


def test_content_hash_unchanged_by_code_header(sample_contract):
    import hashlib, json

    from ateam.contracts.model import Interface

    ifs = [Interface(kind="produces", name="f", signature="def f(a: int) -> int:")]
    c = sample_contract.model_copy(update={"interfaces": ifs})
    # the hash of a contract with interfaces is the same value it was before code_header existed
    data = json.loads(c.model_dump_json(exclude={"status"}))
    assert c.content_hash() == hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert sample_contract.content_hash() == hashlib.sha256(
        json.dumps(json.loads(sample_contract.model_dump_json(exclude={"status", "interfaces"})), sort_keys=True,
                   separators=(",", ":")).encode()).hexdigest()


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
