import pytest

from ateam.contracts.store import ContractStoreError, load_contracts, next_id, save_contract


def test_roundtrip_and_next_id(target_repo, sample_contract):
    d = target_repo / ".ateam/contracts"
    assert next_id(d) == "C-0001"
    p = save_contract(d, sample_contract)
    assert p.name == f"C-0001-{sample_contract.slug()}.yaml"
    assert load_contracts(d)["C-0001"] == sample_contract
    assert next_id(d) == "C-0002"


def test_mismatch_raises(target_repo, sample_contract):
    d = target_repo / ".ateam/contracts"
    p = save_contract(d, sample_contract)
    (d / "C-0009-copy.yaml").write_text(p.read_text())
    with pytest.raises(ContractStoreError, match="C-0009-copy.yaml"):
        load_contracts(d)
