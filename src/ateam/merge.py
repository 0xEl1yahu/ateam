"""ateam merge: the second approval gate. Fast-forward only, in the owner's checkout (owner, 2026-10-25).

Only the committed proposal is trusted: its header names the integration tip the owner approved. A target that moved
since the proposal is refused, never merged or rebased here; `ateam integrate` merges it in and proposes again.
"""

from __future__ import annotations

from dataclasses import dataclass

from ateam.gitops import GitError
from ateam.integrate import integration_branch, proposal_covers
from ateam.intent import IntentError, intent_git, require_clean
from ateam.plans.ops import set_plan_status
from ateam.plans.store import load_plans
from ateam.proposal import ProposalError, parse_header, proposal_relpath


class MergeRefused(Exception):
    """A precondition failed; nothing was changed."""


class FastForwardImpossible(MergeRefused):
    """The target moved since the proposal; nothing was changed."""


@dataclass
class MergeResult:
    plan_id: str
    target_branch: str
    old_head: str
    new_head: str


def merge_plan(ctx, plan_id: str) -> MergeResult:
    paths, db, owner, config = ctx.paths, ctx.db, ctx.git, ctx.config
    plan = load_plans(paths.plans).get(plan_id)
    if plan is None:
        raise MergeRefused(f"unknown plan {plan_id}")
    try:
        require_clean(paths)
    except IntentError as e:
        raise MergeRefused(str(e)) from None
    file_status, stored = str(plan.status), db.feature_status(plan_id)
    replay = file_status == "merged" and stored == "merge_proposed"   # the store step was lost in an interruption
    if not replay and (file_status, stored) != ("merge_proposed", "merge_proposed"):
        raise MergeRefused(f"{plan_id} is {file_status} in its file and {stored} in the state store; merge needs merge_proposed")
    try:
        header = parse_header(intent_git(paths).show_file("HEAD", proposal_relpath(plan_id)))
    except (GitError, ProposalError) as e:
        raise MergeRefused(f"no usable committed merge proposal for {plan_id}: {e}") from None
    branch = integration_branch(plan_id)
    if header.plan_id != plan_id or header.branch != branch:
        raise MergeRefused(f"the committed proposal names {header.plan_id} on {header.branch}, not {plan_id} on {branch}")
    target = config.target_branch
    current = owner.current_branch()
    if current != target:
        raise MergeRefused(f"your checkout is on {current or 'a detached HEAD'}, not {target}; switch to {target} first")
    dirty = owner.status_porcelain()
    if dirty:
        raise MergeRefused(f"your checkout has uncommitted changes ({', '.join(p for _, p in dirty)}); commit or move them first")
    if not owner.branch_exists(branch):
        raise MergeRefused(f"{branch} does not exist")
    tip = owner.rev_parse(branch)
    if tip != header.tip:
        raise MergeRefused(f"{branch} is at {tip[:12]} but the proposal names {header.tip[:12]}; re-run ateam integrate {plan_id}")
    old = owner.rev_parse(target)
    if replay:
        if old != tip:
            raise MergeRefused(f"{plan_id} is merged in its file but {target} is at {old[:12]}, not {tip[:12]}; investigate")
        db.transition("feature", plan_id, "merged", by="merge", reason="reconciled after an interruption")
        return MergeResult(plan_id, target, old, old)
    if old != tip:
        if not owner.is_ancestor(old, tip):
            raise FastForwardImpossible(
                f"{target} moved to {old[:12]} since the proposal; {tip[:12]} does not descend from it, so a fast-forward "
                f"is impossible. Run ateam integrate {plan_id} to merge it in and propose again")
        if not proposal_covers(owner, header, old):
            raise MergeRefused(
                f"{target} is at {old[:12]}, but the proposal was made against {header.target_tip[:12]}; a fast-forward "
                f"would add commits the reviewed diff does not show. Run ateam integrate {plan_id} to propose again")
        owner.merge_ff_only(tip)
    set_plan_status(paths, db, plan, ["merged"], by="merge", why=f"{target} fast-forwarded to {tip}")
    return MergeResult(plan_id, target, old, owner.head())
