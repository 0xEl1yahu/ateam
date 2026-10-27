"""Retry policy (owner, 2026-10-26): classes, retries within budget.attempts, one escalation on error_max_turns."""

import pytest

from ateam.retry import PAUSE_REASONS, Decision, StepOutcome, classify, decide, next_tier


@pytest.mark.parametrize("o,cls", [
    (StepOutcome("build", 0, "completed"), "ok"),
    (StepOutcome("build", 3, "scope_breach"), "scope_breach"),
    (StepOutcome("build", 1, "setup_failed"), "environment"),
    (StepOutcome("build", 1, "failed", subtype="error_max_turns"), "implementation"),
    (StepOutcome("build", 1, "failed", subtype="error_max_budget_usd"), "implementation"),
    (StepOutcome("build", 1, "failed", subtype="error_during_execution"), "environment"),
    (StepOutcome("build", 1, "failed", subtype=None), "environment"),
    (StepOutcome("build", 4, "not_binding"), "not_binding"),
    (StepOutcome("build", 1, "refused"), "refused"),
    (StepOutcome("test", 0, "completed"), "ok"),
    (StepOutcome("test", 5, "completed", test_class="implementation"), "implementation"),
    (StepOutcome("test", 5, "completed", test_class="requirements"), "requirements"),
    (StepOutcome("test", 3, "scope_breach"), "scope_breach"),
    (StepOutcome("verify", 0, "verified", verify_status="verified"), "ok"),
    (StepOutcome("verify", 1, "failed", verify_status="failed", failure_class="implementation"), "implementation"),
    (StepOutcome("verify", 1, "failed", verify_status="failed", failure_class="environment"), "environment"),
    (StepOutcome("verify", 1, "failed", verify_status="failed", failure_class="tamper"), "tamper"),
    (StepOutcome("verify", 1, "refused", verify_status="refused"), "refused"),
    (StepOutcome("verify", 4, "refused", verify_status="refused"), "refused"),
])
def test_classify_table(o, cls):
    assert classify(o) == cls


@pytest.mark.parametrize("cls,used,expected", [
    ("ok", 1, Decision("continue", "standard", None)),
    ("implementation", 1, Decision("retry", "standard", None)),
    ("environment", 1, Decision("retry", "standard", None)),
    ("scope_breach", 2, Decision("retry", "standard", None)),
    ("implementation", 3, Decision("pause", "standard", "budget_exhausted")),
    ("environment", 3, Decision("pause", "standard", "environment_failure")),
    ("scope_breach", 3, Decision("pause", "standard", "scope_exceeded")),
    ("requirements", 1, Decision("pause", "standard", "decision_change_needed")),
    ("refused", 1, Decision("pause", "standard", "environment_failure")),
    ("drifted", 1, Decision("pause", "standard", "records_drifted")),
    ("tamper", 1, Decision("pause", "standard", "verification_unresolvable")),
    ("not_binding", 1, Decision("pause", "standard", "verification_unresolvable")),
])
def test_decide_table(cls, used, expected):
    assert decide(cls, build_attempts_used=used, attempts_allowed=3, tier="standard", escalated=False, last_subtype=None) == expected


def test_escalation_once_and_never_above_careful():
    up = decide("implementation", 1, 3, "standard", escalated=False, last_subtype="error_max_turns")
    assert up == Decision("retry", "careful", None)
    assert decide("implementation", 2, 3, "standard", escalated=True, last_subtype="error_max_turns").tier == "standard"
    assert decide("implementation", 1, 3, "careful", escalated=False, last_subtype="error_max_turns").tier == "careful"
    assert decide("environment", 1, 3, "light", escalated=False, last_subtype="error_max_turns").tier == "light"
    assert decide("implementation", 1, 3, "light", escalated=False, last_subtype="error_max_budget_usd").tier == "light"
    assert next_tier("light") == "standard" and next_tier("careful") is None


def test_pause_reasons_are_the_spec_enum():
    assert PAUSE_REASONS == {"decision_change_needed", "scope_exceeded", "budget_exhausted", "verification_unresolvable",
                             "sensitive_action", "environment_failure", "records_drifted", "owner_stopped"}
    for cls in ("implementation", "environment", "scope_breach", "requirements", "refused", "tamper", "not_binding", "drifted"):
        d = decide(cls, 3, 3, "standard", False, None)
        assert d.action == "pause" and d.reason in PAUSE_REASONS


def test_environment_at_test_or_verify_retries_that_stage():
    assert decide("environment", 1, 2, "light", False, None, stage="test", stage_used=0) == Decision("retry_stage", "light", None)
    assert decide("environment", 1, 2, "light", False, None, stage="verify", stage_used=1) == Decision("retry_stage", "light", None)
    assert decide("environment", 1, 2, "light", False, None, stage="test", stage_used=2) == Decision("pause", "light", "environment_failure")
    assert decide("implementation", 1, 2, "light", False, None, stage="test", stage_used=0).action == "retry"   # back to BUILD


def test_a_tampered_session_classifies_as_tamper():
    assert classify(StepOutcome("build", 0, "tamper")) == "tamper"
    assert classify(StepOutcome("test", 0, "tamper")) == "tamper"


@pytest.mark.parametrize("stage,code,outcome,status,refusal,reason", [
    ("verify", 4, "refused", "refused", "records_drifted", "records_drifted"),
    ("verify", 4, "refused", "refused", "not_binding", "verification_unresolvable"),
    ("verify", 4, "refused", "refused", "intent_dirty", "environment_failure"),
    ("verify", 1, "refused", "refused", "other", "environment_failure"),
    ("build", 4, "not_binding", None, "records_drifted", "records_drifted"),
    ("build", 4, "not_binding", None, "not_binding", "verification_unresolvable"),
    ("build", 1, "refused", None, "other", "environment_failure"),
])
def test_refusals_pause_with_their_cause(stage, code, outcome, status, refusal, reason):
    o = StepOutcome(stage, code, outcome, verify_status=status, refusal=refusal)
    assert decide(classify(o), 0, 2, "light", False, None, stage=stage, stage_used=1).reason == reason


def test_owner_stopped_is_a_pause_reason():
    assert "owner_stopped" in PAUSE_REASONS
