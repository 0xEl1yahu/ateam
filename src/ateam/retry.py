"""Retry policy for `ateam run` (spec "Allocation" failure classes; owner rulings of 2026-10-26).

Pure functions: an outcome of one step (BUILD, TEST or verify, as their result files report it) becomes a class,
and a class plus the attempt history becomes a decision. A retry is always a fresh BUILD attempt; the limit is the
contract's frozen budget.attempts BUILD attempts, counted across runs. One escalation, one tier up, once per contract,
only after a BUILD that ended with subtype error_max_turns; environment failures never change the tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ateam.tiers import ORDER

PAUSE_REASONS = {"decision_change_needed", "scope_exceeded", "budget_exhausted", "verification_unresolvable",
                 "sensitive_action", "environment_failure", "records_drifted", "owner_stopped"}

ESCALATION_EVIDENCE = "error_max_turns"
_IMPLEMENTATION_SUBTYPES = {"error_max_turns", "error_max_budget_usd"}   # the session ran out of turns or money
_PAUSE_NOW = {"requirements": "decision_change_needed", "drifted": "records_drifted", "not_binding": "verification_unresolvable",
              "refused": "environment_failure", "tamper": "verification_unresolvable"}
_EXHAUSTED = {"environment": "environment_failure", "scope_breach": "scope_exceeded", "implementation": "budget_exhausted"}


@dataclass(frozen=True)
class StepOutcome:
    stage: Literal["build", "test", "verify"]
    code: int
    outcome: str
    subtype: str | None = None
    test_class: str | None = None
    verify_status: str | None = None
    failure_class: str | None = None
    refusal: str | None = None   # records_drifted, not_binding, intent_dirty or other, from the result file


@dataclass(frozen=True)
class Decision:
    action: Literal["continue", "retry", "retry_stage", "pause"]   # retry: a fresh BUILD; retry_stage: the same stage again
    tier: str
    reason: str | None


def next_tier(tier: str) -> str | None:
    i = ORDER.index(tier)
    return ORDER[i + 1] if i + 1 < len(ORDER) else None


def classify(o: StepOutcome) -> str:
    """ok, implementation, environment, requirements, tamper, drifted, not_binding, refused or scope_breach."""
    if o.stage == "verify":
        if o.verify_status == "verified":
            return "ok"
        if o.verify_status == "refused":
            return {"records_drifted": "drifted", "not_binding": "not_binding"}.get(o.refusal or "", "refused")
        return {"tamper": "tamper", "environment": "environment"}.get(o.failure_class or "", "implementation")
    if o.outcome == "tamper":
        return "tamper"
    if o.refusal == "records_drifted":
        return "drifted"
    if o.outcome == "not_binding" or o.code == 4:
        return "not_binding"
    if o.outcome == "setup_failed":
        return "environment"
    if o.outcome == "refused":
        return "refused"
    if o.code == 3:
        return "scope_breach"
    if o.code == 5:
        return "requirements" if o.test_class == "requirements" else "implementation"
    if o.code == 0:
        return "ok"
    return "implementation" if o.subtype in _IMPLEMENTATION_SUBTYPES else "environment"


def decide(cls: str, build_attempts_used: int, attempts_allowed: int, tier: str, escalated: bool,
           last_subtype: str | None, stage: str = "build", stage_used: int = 0) -> Decision:
    """An environment failure at TEST or verify retries that stage, up to attempts_allowed times, without a new BUILD
    (owner, 2026-10-26); every other failure after BUILD is a fresh BUILD attempt."""
    if cls == "ok":
        return Decision("continue", tier, None)
    if cls in _PAUSE_NOW:
        return Decision("pause", tier, _PAUSE_NOW[cls])
    if cls == "environment" and stage in ("test", "verify"):
        return Decision("pause", tier, "environment_failure") if stage_used >= attempts_allowed else Decision("retry_stage", tier, None)
    if build_attempts_used >= attempts_allowed:
        return Decision("pause", tier, _EXHAUSTED.get(cls, "budget_exhausted"))
    up = next_tier(tier)
    if cls == "implementation" and last_subtype == ESCALATION_EVIDENCE and not escalated and up is not None:
        return Decision("retry", up, None)
    return Decision("retry", tier, None)
