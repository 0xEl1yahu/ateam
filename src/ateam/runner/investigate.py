"""An INVESTIGATE session run in place: discovery and planning in the owner's checkout, a pause investigation in
the paused contract's worktree (owner, 2026-10-26).

Read-only is enforced before anything runs (tool list, permission callback, PreToolUse hook)
and verified afterwards: a snapshot of the checkout and of the intent repo must be unchanged,
or the attempt is recorded as tamper and its report discarded.
"""

from __future__ import annotations

from dataclasses import dataclass

from ateam.config import TargetConfig
from ateam.events import open_event_log
from ateam.gitops import GitRepo
from ateam.intent import intent_git
from ateam.paths import TargetPaths
from ateam.roles import Role, RoleSpec
from ateam.runner.options import worker_options
from ateam.runner.policy import Policy
from ateam.runner.run import WorkerResult, prepare_scratch, run_worker
from ateam.runner.snapshot import changed, take
from ateam.state.db import StateDB
from ateam.tiers import TierSpec


INTENT_TAMPER = "intent repo changed during the session"


@dataclass
class InvestigationResult:
    res: WorkerResult
    attempt_id: int
    tampered: list[str]


async def run_investigation(
    *, paths: TargetPaths, config: TargetConfig, target_git: GitRepo, name: str, spec: RoleSpec, prompt: str,
    brief_text: str, tier: TierSpec, db: StateDB, tier_name: str = "custom", resume: str | None = None,
    scratch_name: str | None = None,
) -> InvestigationResult:
    if spec.role != Role.investigate:
        raise ValueError("run_investigation only runs INVESTIGATE specs")
    scratch = prepare_scratch(paths, scratch_name or name)
    checkout = target_git.root   # the owner's checkout (init, plan) or a contract worktree (a pause investigation)
    policy = Policy(
        worktree=checkout, role=Role.investigate, allowed_paths=[], protected=list(config.protected_paths),
        bound={}, scratch_dir=scratch, ateam_home=scratch.parent.parent.parent,
    )
    intent = intent_git(paths)
    intent_before = (intent.head(), intent.is_clean())
    checkout_before = take(target_git)
    attempt_id = db.start_attempt(name, Role.investigate.value, tier_name, tier.model, {})
    row = db.attempt(attempt_id)
    events = open_event_log(paths, attempt_id, name, Role.investigate.value, row["started_at"] if row else "")
    policy.events = events
    try:
        options = worker_options(cwd=checkout, spec=spec, brief_text=brief_text, policy=policy, tier=tier, resume=resume)
        res = await run_worker(prompt, options, policy)
        tampered = changed(checkout_before, take(target_git))
        intent_after = (intent.head(), intent.is_clean())
        if intent_after != intent_before or not intent_after[1]:
            tampered.append(INTENT_TAMPER)
    except Exception:
        db.finish_attempt(attempt_id, "failed", "environment", 0, 0, 0.0, 0, 0.0, None)
        raise
    finally:
        events.close()
    usage = res.usage
    tokens_in = int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_read_input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    if tampered:
        res.report = None
        outcome, failure_class = "tamper", "tamper"
    else:
        outcome, failure_class = ("completed" if res.report else "failed"), None
    db.finish_attempt(attempt_id, outcome, failure_class, tokens_in, tokens_out, res.cost_usd, res.num_turns, res.wall_s, res.session_id)
    return InvestigationResult(res=res, attempt_id=attempt_id, tampered=tampered)
