from ateam.records.model import Binds, Evidence, Status
from ateam.records.validate import validate_records


def errors(problems):
    return [p for p in problems if p.level == "error"]


def test_clean_set_has_no_errors(target_repo, records_three):
    assert errors(validate_records(records_three, target_repo)) == []


def test_invalid_glob_is_error(target_repo, records_three):
    records_three["R-0001"].binds = Binds(paths=["./src/**"])
    assert any("invalid glob" in p.message and p.record_id == "R-0001" for p in errors(validate_records(records_three, target_repo)))


def test_documented_needs_doc_evidence(target_repo, records_three):
    records_three["R-0001"].evidence = [Evidence(type="code", ref="src/app.py")]
    assert any("documented" in p.message for p in errors(validate_records(records_three, target_repo)))


def test_dangling_and_cycle_are_errors(target_repo, records_three):
    records_three["R-0001"].supersedes = ["R-0099"]
    assert any("R-0099" in p.message for p in errors(validate_records(records_three, target_repo)))
    records_three["R-0001"].supersedes = ["R-0002"]; records_three["R-0002"].supersedes = ["R-0001"]
    assert any("cycle" in p.message for p in errors(validate_records(records_three, target_repo)))


def test_supersession_incomplete(target_repo, records_three):
    records_three["R-0001"].supersedes = ["R-0002"]  # R-0002 still approved
    assert any("supersession incomplete" in p.message for p in errors(validate_records(records_three, target_repo)))


def test_superseded_without_superseder(target_repo, records_three):
    records_three["R-0002"].status = Status.superseded
    assert any("no superseder" in p.message for p in errors(validate_records(records_three, target_repo)))


def test_dead_glob_is_warning(target_repo, records_three):
    records_three["R-0001"].binds = Binds(paths=["src/nothing/**"])
    probs = validate_records(records_three, target_repo)
    assert any("dead glob" in p.message and p.level == "warning" for p in probs) and errors(probs) == []


def test_duplicate_serves_is_warning(target_repo, records_three):
    records_three["R-0002"].serves = ["friendly api"]
    assert any("friendly api" in p.message and p.level == "warning" for p in validate_records(records_three, target_repo))


def test_hand_edit_detected(target_repo, records_three):
    hashes = {rid: r.content_hash() for rid, r in records_three.items()}
    records_three["R-0001"].statement += " changed"
    assert any("hand edit" in p.message and p.record_id == "R-0001" for p in validate_records(records_three, target_repo, hashes))
