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


def test_plan_new_fields_default_and_hash():
    p = Plan(id="P-0001", title="T", request="r")
    assert p.depends_on_plans == [] and p.files_read == [] and p.out_of_scope == [] and p.verification_approach == ""
    h = p.content_hash()
    for name, value in (("depends_on_plans", ["P-0002"]), ("files_read", ["a.py"]), ("out_of_scope", ["x"]), ("verification_approach", "run tests")):
        assert p.model_copy(update={name: value}).content_hash() != h, name


def test_review_round_trips_and_leaves_content_hash_unchanged():
    from ateam.plans.model import PlanReview, ReviewFinding
    p = Plan(id="P-0001", title="T", request="r", contracts=["C-0001"])
    assert p.review is None
    assert p.content_hash() == "9946c1c0480fa3267a955ec331f1bb809da3c07c272859dfd7cd0cc03c09087a"   # the hash before reviews existed
    review = PlanReview(verdict="needs_revision", revised=True, session_id="s2", cost_usd=0.5,
                        findings=[ReviewFinding(contract="C-0001", severity="blocking", finding="f", suggestion="s")])
    q = p.model_copy(update={"review": review})
    assert q.content_hash() == p.content_hash()
    assert Plan.model_validate(q.model_dump(mode="json")).review == review


def test_review_notes_default_empty_and_round_trip():
    from ateam.plans.model import PlanReview
    assert PlanReview(verdict="unavailable").notes == ""
    r = PlanReview(verdict="defensible", notes="looked at routes.py")
    assert PlanReview.model_validate(r.model_dump(mode="json")).notes == "looked at routes.py"
