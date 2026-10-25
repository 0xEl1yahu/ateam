import pytest

from ateam.contracts.store import ContractStoreError, load_contracts, next_id, save_contract


def test_roundtrip_and_next_id(target_repo, target_paths, sample_contract):
    d = target_paths.contracts
    assert next_id(d) == "C-0001"
    p = save_contract(d, sample_contract)
    assert p.name == f"C-0001-{sample_contract.slug()}.yaml"
    assert load_contracts(d)["C-0001"] == sample_contract
    assert next_id(d) == "C-0002"


def test_mismatch_raises(target_repo, target_paths, sample_contract):
    d = target_paths.contracts
    p = save_contract(d, sample_contract)
    (d / "C-0009-copy.yaml").write_text(p.read_text())
    with pytest.raises(ContractStoreError, match="C-0009-copy.yaml"):
        load_contracts(d)


def test_malformed_contract_file_raises_store_error_naming_file(target_repo, target_paths):
    d = target_paths.contracts
    (d / "C-0002-bad.yaml").write_text("id: C-0002\ntitle: x\n")
    with pytest.raises(ContractStoreError, match="C-0002-bad.yaml"):
        load_contracts(d)
