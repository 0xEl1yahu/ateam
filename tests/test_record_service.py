import pytest

from ateam.gitops import GitRepo
from ateam.records.lifecycle import LifecycleError
from ateam.records.model import Status
from ateam.records.store import load_records, save_record


def test_approve_commits_only_ateam(record_service, target_repo):
    (target_repo / "src/app.py").write_text("dirty\n")
    r = record_service.approve("R-0001")
    g = GitRepo(target_repo)
    assert r.status == Status.approved
    assert g.run("log", "-1", "--format=%s").strip() == "ateam: approve R-0001"
    assert not g.is_clean(["src"]) and g.is_clean([".ateam"])
    assert load_records(target_repo / ".ateam/records")["R-0001"].status == Status.approved


def test_refuses_on_other_branch(record_service, target_repo):
    GitRepo(target_repo).run("checkout", "-q", "-b", "other")
    with pytest.raises(LifecycleError, match="other"):
        record_service.approve("R-0001")


def test_refuses_when_ateam_dirty(record_service, target_repo):
    (target_repo / ".ateam/records/stray.yaml").write_text("x: 1\n")
    with pytest.raises(LifecycleError, match="uncommitted"):
        record_service.approve("R-0001")


def test_supersede_commits_both(record_service, target_repo, sample_record):
    record_service.approve("R-0001")
    save_record(target_repo / ".ateam/records", sample_record.model_copy(update={"id": "R-0002"}))
    GitRepo(target_repo).commit_paths([".ateam"], "seed R-0002")
    old, new = record_service.supersede("R-0001", "R-0002")
    recs = load_records(target_repo / ".ateam/records")
    assert recs["R-0001"].status == Status.superseded and recs["R-0002"].status == Status.approved
    assert GitRepo(target_repo).run("log", "-1", "--format=%s").strip() == "ateam: supersede R-0001 with R-0002"
