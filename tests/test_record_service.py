import pytest

from ateam.gitops import GitRepo
from ateam.intent import commit_intent
from ateam.records.lifecycle import LifecycleError
from ateam.records.model import Status
from ateam.records.store import load_records, save_record


def test_approve_commits_in_intent_repo_not_target(record_service, target_repo, target_paths):
    target_head = GitRepo(target_repo).head()
    (target_repo / "src/app.py").write_text("dirty\n")
    r = record_service.approve("R-0001")
    intent = GitRepo(target_paths.intent)
    assert r.status == Status.approved
    assert intent.run("log", "-1", "--format=%s").strip() == "ateam: approve R-0001" and intent.is_clean()
    assert GitRepo(target_repo).head() == target_head
    assert load_records(target_paths.records)["R-0001"].status == Status.approved


def test_approve_no_longer_requires_target_branch(record_service, target_repo):
    GitRepo(target_repo).run("checkout", "-q", "-b", "other")
    assert record_service.approve("R-0001").status == Status.approved


def test_refuses_when_intent_dirty(record_service, target_paths):
    (target_paths.records / "stray.yaml").write_text("x: 1\n")
    with pytest.raises(LifecycleError, match=str(target_paths.intent)):
        record_service.approve("R-0001")


def test_supersede_commits_both(record_service, target_paths, sample_record):
    record_service.approve("R-0001")
    save_record(target_paths.records, sample_record.model_copy(update={"id": "R-0002"}))
    commit_intent(target_paths, "seed R-0002", ["records"])
    old, new = record_service.supersede("R-0001", "R-0002")
    recs = load_records(target_paths.records)
    assert recs["R-0001"].status == Status.superseded and recs["R-0002"].status == Status.approved
    assert GitRepo(target_paths.intent).run("log", "-1", "--format=%s").strip() == "ateam: supersede R-0001 with R-0002"


def test_approve_refuses_record_with_validation_errors(record_service, target_paths, sample_record):
    bad = sample_record.model_copy(update={"id": "R-0002"})
    bad.binds.paths = ["./src/**"]
    save_record(target_paths.records, bad)
    commit_intent(target_paths, "seed bad", ["records"])
    with pytest.raises(LifecycleError, match="invalid glob"):
        record_service.approve("R-0002")
    assert load_records(target_paths.records)["R-0002"].status == Status.proposed
