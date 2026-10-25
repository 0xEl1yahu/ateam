from datetime import UTC, datetime

from ateam.plans.model import Plan, PlanHistoryEntry, PlanStatus
from ateam.state.db import FEATURE_TRANSITIONS


def test_plan_defaults_and_slug():
    p = Plan(id="P-0001", title="Add a health function to the routes module", request="add health")
    assert p.status == PlanStatus.requested and p.contracts == [] and p.proposed_records == [] and p.unknowns == []
    assert p.tier_overrides == {} and p.history == []
    assert p.slug() == "add-a-health-function-to-the"  # first six words, same rule as contracts


def test_statuses_match_feature_transitions():
    names = {s for s in FEATURE_TRANSITIONS if s} | {t for ts in FEATURE_TRANSITIONS.values() for t in ts}
    assert {s.value for s in PlanStatus} == names


def test_content_hash_ignores_status_and_history():
    p = Plan(id="P-0001", title="T", request="r", contracts=["C-0001"])
    h = p.content_hash()
    q = p.model_copy(update={"status": PlanStatus.plan_approved, "history": [PlanHistoryEntry(at=datetime.now(UTC), by="owner", to=PlanStatus.plan_approved)]})
    assert q.content_hash() == h
    assert p.model_copy(update={"contracts": ["C-0002"]}).content_hash() != h
