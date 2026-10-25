from ateam.contracts.model import ContractStatus
from ateam.contracts.select import drift, freeze, select_records
from ateam.records.graph import RecordGraph
from ateam.records.lifecycle import approve
from ateam.records.model import Status


def _all_approved(records):
    return {k: (approve(v) if v.status == Status.proposed else v) for k, v in records.items()}


def test_select_by_file_overlap_and_globals(target_repo, records_three):
    g = RecordGraph(_all_approved(records_three))
    assert [r.id for r in select_records(["src/api/x.py"], g, target_repo)] == ["R-0001", "R-0002", "R-0003"]
    assert [r.id for r in select_records(["docs/**"], g, target_repo)] == ["R-0002"]


def test_select_ignores_proposed(target_repo, records_three):
    assert [r.id for r in select_records(["src/app.py"], RecordGraph(records_three), target_repo)] == ["R-0002"]


def test_select_by_glob_prefix_for_new_files(target_repo, records_three):
    g = RecordGraph(records_three)
    assert "R-0001" in [r.id for r in select_records(["src/api/new/**"], g, target_repo)]
    assert "R-0001" in [r.id for r in select_records(["src/**"], g, target_repo)]


def test_freeze_and_drift(sample_contract, records_three):
    frozen = freeze(sample_contract, [records_three["R-0001"]])
    assert frozen.status == ContractStatus.ready
    assert frozen.records[0].id == "R-0001" and frozen.records[0].hash == records_three["R-0001"].content_hash()
    assert drift(frozen, records_three) == []
    records_three["R-0001"].statement += " x"
    assert drift(frozen, records_three) == ["R-0001"]
    assert drift(frozen, {}) == ["R-0001"]


def test_freeze_with_dependencies_stays_draft(sample_contract):
    c = sample_contract.model_copy(update={"depends_on": ["C-0000"]})
    assert freeze(c, []).status == ContractStatus.draft
