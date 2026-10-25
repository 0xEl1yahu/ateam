import pytest

from ateam.records.lifecycle import LifecycleError, approve, reject, retire, supersede
from ateam.records.model import Basis, Status


def test_approve_sets_status_basis_history(sample_record):
    r = approve(sample_record, basis=Basis.observation)
    assert r.status == Status.approved and r.basis == Basis.observation
    assert r.history[-1].to == Status.approved and r.history[-1].by == "owner"
    assert sample_record.status == Status.proposed  # pure: input untouched


def test_approve_rejects_non_proposed(sample_record):
    with pytest.raises(LifecycleError):
        approve(approve(sample_record))


def test_reject_keeps_reason(sample_record):
    r = reject(sample_record, "already covered by tests")
    assert r.status == Status.rejected and r.history[-1].why == "already covered by tests"


def test_supersede_is_one_transition(sample_record):
    old = approve(sample_record)
    new = sample_record.model_copy(update={"id": "R-0002"})
    old2, new2 = supersede(old, new)
    assert old2.status == Status.superseded and new2.status == Status.approved and "R-0001" in new2.supersedes
    assert old2.history[-1].why == "superseded by R-0002"


def test_supersede_requires_states(sample_record):
    with pytest.raises(LifecycleError):
        supersede(sample_record, sample_record.model_copy(update={"id": "R-0002"}))  # old not approved


def test_retire_requires_approved(sample_record):
    with pytest.raises(LifecycleError):
        retire(sample_record, "obsolete")
    assert retire(approve(sample_record), "obsolete").status == Status.retired
