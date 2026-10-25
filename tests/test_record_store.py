import pytest
import yaml

from ateam.records.store import RecordStoreError, load_records, next_id, save_record


def test_roundtrip_and_filename(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    p = save_record(d, sample_record)
    assert p.name == f"{sample_record.id}-{sample_record.slug()}.yaml"
    assert load_records(d)[sample_record.id] == sample_record


def test_save_replaces_old_slug_file(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    save_record(d, sample_record)
    save_record(d, sample_record.model_copy(update={"statement": "Completely new statement here"}))
    assert len(list(d.glob("R-0001-*.yaml"))) == 1


def test_next_id(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    assert next_id(d) == "R-0001"
    save_record(d, sample_record.model_copy(update={"id": "R-0007"}))
    assert next_id(d) == "R-0008"


def test_id_filename_mismatch_raises(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    (d / "R-0002-wrong.yaml").write_text(yaml.safe_dump(sample_record.model_dump(mode="json")))
    with pytest.raises(RecordStoreError, match="R-0002-wrong.yaml"):
        load_records(d)


def test_duplicate_id_raises(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    p = save_record(d, sample_record)
    (d / f"{sample_record.id}-copy.yaml").write_text(p.read_text())
    with pytest.raises(RecordStoreError, match="duplicate"):
        load_records(d)


def test_malformed_record_file_raises_store_error_naming_file(target_repo):
    d = target_repo / ".ateam/records"
    (d / "R-0003-bad.yaml").write_text("id: R-0003\nkind: nonsense\n")
    with pytest.raises(RecordStoreError, match="R-0003-bad.yaml"):
        load_records(d)
