"""ateam integrate: merge a plan's verified contracts onto ateam/<plan-id>, re-check, and propose the merge.

Per the spec's "Verification and integration" and the owner's 2026-10-26 rulings: the integration branch starts at
the target tip recorded before the branch is made; contracts merge with merge commits in dependency order; a moved
target is merged in, never rebased; a conflict, a setup failure or a failed final check pauses the feature. The
intent repo is written first and the state store follows, and a re-run replays exactly the store steps a crash lost.
Nothing is pushed, fetched or deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ateam import verify as v
from ateam.config import NamedCheck, TargetConfig
from ateam.contracts.model import Contract, ContractStatus
from ateam.contracts.select import drift
from ateam.contracts.store import load_contracts, save_contract
from ateam.gitops import GitError, GitRepo, MergeConflict
from ateam.intent import IntentError, commit_intent_files, intent_git, require_clean
from ateam.paths import TargetPaths, home_dir
from ateam.plans.model import Plan
from ateam.plans.ops import dependency_order, plan_contract_ids, set_plan_status
from ateam.plans.store import load_plans
from ateam.proposal import ProposalError, ProposalHeader, parse_header, proposal_relpath, render_proposal
from ateam.records.store import load_records
from ateam.runner.run import SetupError, ensure_setup
from ateam.tiers import load_tiers

WALK = ["plan_approved", "executing", "integrating", "merge_proposed"]
# Movement outside the integration worktree during the final checks: something else committed in parallel. The run is
# refused for a retry instead of pausing the plan (owner, 2026-10-26); tamper inside the worktree still pauses.
RETRY_TAMPER = {"intent repo HEAD moved", "intent repo files changed", "owner checkout HEAD moved"}


class IntegrateRefused(Exception):
    """A precondition failed; nothing was changed by this run."""


@dataclass
class IntegrateResult:
    outcome: Literal["merge_proposed", "conflict", "checks_failed", "setup_failed"]
    plan_id: str
    tip: str | None = None
    proposal: Path | None = None
    messages: list[str] = field(default_factory=list)


def integration_branch(plan_id: str) -> str:
    return f"ateam/{plan_id}"


def integration_worktree(paths: TargetPaths, plan_id: str) -> Path:
    return home_dir(paths.root) / "worktrees" / plan_id


def proposal_covers(git: GitRepo, header: ProposalHeader, target_tip: str) -> bool:
    """The target is where the proposal saw it, or has moved forward along the proposed history only: everything a
    fast-forward to header.tip adds was in the reviewed diff (owner, 2026-10-26)."""
    return target_tip == header.target_tip or (
        git.is_ancestor(header.target_tip, target_tip) and git.is_ancestor(target_tip, header.tip))


def committed_header(paths: TargetPaths, plan_id: str) -> ProposalHeader:
    try:
        return parse_header(intent_git(paths).show_file("HEAD", proposal_relpath(plan_id)))
    except (GitError, ProposalError) as e:
        raise IntegrateRefused(f"no usable committed merge proposal for {plan_id}: {e}") from None


def check_set(config: TargetConfig, contracts: list[Contract]) -> list[NamedCheck]:
    """config.checks plus every contract's checks, de-duplicated by (name, cmd), in that order."""
    seen: set[tuple[str, str]] = set()
    out: list[NamedCheck] = []
    for c in [*config.checks, *(ch for k in contracts for ch in k.checks)]:
        if (c.name, c.cmd) not in seen:
            seen.add((c.name, c.cmd))
            out.append(c)
    return out


def integrate(ctx, plan_id: str, echo: Callable[[str], None]) -> IntegrateResult:
    """`ctx` carries paths, git (the owner's checkout), config and db, as the CLI's Context does."""
    paths, db, owner, config = ctx.paths, ctx.db, ctx.git, ctx.config
    plan = load_plans(paths.plans).get(plan_id)
    if plan is None:
        raise IntegrateRefused(f"unknown plan {plan_id}")
    if db.feature_status(plan_id) == "paused":
        last = db.transitions("feature", plan_id)[-1]["reason"] or "no reason recorded"
        raise IntegrateRefused(f"{plan_id} is paused ({last}); resolve it before integrating again")
    try:
        require_clean(paths)
    except IntentError as e:
        raise IntegrateRefused(str(e)) from None
    branch = integration_branch(plan_id)
    cs = load_contracts(paths.contracts)
    cids = plan_contract_ids(db, plan)
    _preflight(db, cs, cids, load_records(paths.records))
    checks = check_set(config, [cs[c] for c in cids])
    if not checks:
        raise IntegrateRefused(f"{plan_id} has no deterministic checks (config.checks or the contracts'); integrate needs at least one")
    reconcile_contracts(db, owner, cs, cids, branch, echo)
    store = reconcile_feature(paths, db, plan, echo)

    target_tip = owner.rev_parse(config.target_branch)
    if store == "merge_proposed":
        try:
            tip = owner.rev_parse(branch)
        except GitError:
            raise IntegrateRefused(f"{branch} is gone; reject {plan_id} or restore the branch at the proposed tip") from None
        header = committed_header(paths, plan_id)
        if header.tip == tip and proposal_covers(owner, header, target_tip):
            _require_heads(db, owner, cids, tip, branch)
            return IntegrateResult("merge_proposed", plan_id, tip, paths.intent / proposal_relpath(plan_id),
                                   [f"already proposed; {config.target_branch} has not moved"])
        why = (f"{branch} has commits the proposal does not cover" if header.tip != tip
               else f"{config.target_branch} moved to {target_tip[:12]} after the proposal")
        plan = set_plan_status(paths, db, plan, ["integrating"], by="integrate", why=why)
        echo(f"{plan_id} -> integrating ({why})")
    elif store != "integrating":
        plan = set_plan_status(paths, db, plan, WALK[WALK.index(store) + 1:WALK.index("integrating") + 1], by="integrate")
        echo(f"{plan_id} -> integrating")

    wt = _prepare(paths, db, owner, config, plan_id, branch, target_tip, echo)
    try:
        ensure_setup(wt.root, config, db, json.loads(db.get_meta(f"integration:{plan_id}"))["start"],
                     on_command=lambda c: echo(f"setup: {c}"))
    except SetupError as e:
        return _pause(paths, db, plan, "setup_failed", f"environment_failure: setup failed: {e.cmd}",
                      [line for line in e.output.splitlines()[-20:]], echo)

    for cid in dependency_order({c: cs[c] for c in cids}):
        if cs[cid].status == ContractStatus.integrated:
            continue
        try:
            merge_contract(ctx, wt, plan_id, cid, echo)
        except MergeConflict as e:
            why = f"environment_failure: {cid} conflicts in {', '.join(e.paths)}"
            paused = [{"contract_id": cid, "reason": "environment_failure", "detail": why.split(": ", 1)[1],
                       "contract_hash": db.contract_hash(cid), "evidence": {"paths": list(e.paths)}}]
            return _pause(paths, db, plan, "conflict", why, [], echo, contracts=paused)

    _require_heads(db, wt, cids, "HEAD", branch)
    if not wt.is_ancestor(target_tip, "HEAD"):
        try:
            wt.merge_no_ff(target_tip, f"ateam: merge {config.target_branch} into {branch}")
        except MergeConflict as e:
            return _pause(paths, db, plan, "conflict",
                          f"environment_failure: {config.target_branch} conflicts in {', '.join(e.paths)}", [], echo)
        echo(f"merged {config.target_branch} ({target_tip[:12]}) into {branch}")

    intent = intent_git(paths)

    def fingerprint() -> str:
        h = hashlib.sha256("".join(db.fingerprint(c, records=False) for c in cids).encode())
        h.update(json.dumps(db.transitions("feature", plan_id), default=str).encode())
        return h.hexdigest()

    before = v.snapshot(wt, intent, owner, fingerprint())
    results = v.run_checks(checks, wt.root, v.check_env(os.environ, config.env))
    after = v.snapshot(wt, intent, owner, fingerprint())
    tamper, warnings = v.compare(before, after)
    tails: list[str] = []
    for r in results:
        echo(f"  {r.name}: " + ("passed" if r.passed else "errored" if r.errored else "failed"))
        if not r.passed:
            tails += ["      " + line for line in r.tail.splitlines()[-20:]]
    for line in tails:
        echo(line)
    for t in tamper:
        echo(f"  tamper: {t}")
    if tamper and set(tamper) <= RETRY_TAMPER:
        raise IntegrateRefused(f"something else committed during the final checks ({'; '.join(tamper)}); nothing was "
                               f"paused. Re-run ateam integrate {plan_id} when nothing else is running")
    failed = [r.name for r in results if not r.passed]
    if failed or tamper:
        detail = "; ".join([*(f"{n} failed" for n in failed), *(f"tamper: {t}" for t in tamper)])
        return _pause(paths, db, plan, "checks_failed", f"verification_unresolvable: {detail}", tails, echo)

    tip = wt.head()
    text = render_proposal(
        ProposalHeader(plan_id, branch, tip, config.target_branch, target_tip),
        diff_stat=owner.diff_stat(target_tip, tip),
        contracts=[_evidence(db, cs[c]) for c in dependency_order({c: cs[c] for c in cids})],
        records=_records(paths, plan, cs, cids),
        risks=_risks(db, plan, cs, cids, warnings),
        telemetry=[a for a in db.telemetry_rows() if a["contract_id"] in cids],
        final_checks=[r.fact() for r in results],
        tiers=load_tiers(ctx.config.tiers),
    )
    proposal = paths.intent / proposal_relpath(plan_id)
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text(text)
    set_plan_status(paths, db, plan, ["merge_proposed"], by="integrate", extra_files=[proposal])
    echo(f"{plan_id} -> merge_proposed ({tip[:12]}); proposal: {proposal}")
    return IntegrateResult("merge_proposed", plan_id, tip, proposal)


def _preflight(db, cs: dict[str, Contract], cids: list[str], records: dict) -> None:
    if not cids:
        raise IntegrateRefused("the plan has no contracts")
    for cid in cids:
        c = cs.get(cid)
        if c is None:
            raise IntegrateRefused(f"{cid} has no contract file")
        stored = db.contract_status(cid)
        if c.status not in ("verified", "integrated") or stored not in ("verified", "integrated"):
            raise IntegrateRefused(f"{cid} is {c.status} in its file and {stored} in the state store; integrate needs every contract verified")
        if c.content_hash() != db.contract_hash(cid):
            raise IntegrateRefused(f"{cid} differs from its frozen hash (hand edit after freeze)")
        drifted = drift(c, records)
        if drifted:
            raise IntegrateRefused(f"{cid} has record drift: {', '.join(drifted)}")
        cp = db.latest_checkpoint(cid)
        if not cp or not cp.get("head"):
            raise IntegrateRefused(f"{cid} has no verify checkpoint naming its head")


def prepare_integration(ctx, plan_id: str, target_tip: str, echo) -> GitRepo:
    """Record the integration start (once), make or check ateam/<plan-id>, and make or check its worktree."""
    return _prepare(ctx.paths, ctx.db, ctx.git, ctx.config, plan_id, integration_branch(plan_id), target_tip, echo)


def merge_contract(ctx, wt: GitRepo, plan_id: str, cid: str, echo) -> str:
    """Merge a verified contract's head onto the integration branch and record it integrated: the contract file in
    an intent commit first, then the store. A repeat, or a file already integrated by an interrupted run, records
    only what is missing. A conflict raises MergeConflict with the merge aborted and nothing recorded."""
    paths, db = ctx.paths, ctx.db
    contract = load_contracts(paths.contracts)[cid]
    head = db.latest_checkpoint(cid)["head"]
    if wt.is_ancestor(head, "HEAD"):
        merged = [t["reason"] for t in db.transitions("contract", cid) if (t.get("reason") or "").startswith("merged ")]
        merge = merged[-1].removeprefix("merged ") if merged else wt.head()   # merged before an interruption
    else:
        merge = wt.merge_no_ff(head, f"ateam: integrate {cid} into {integration_branch(plan_id)}")
    if contract.status != ContractStatus.integrated:
        saved = save_contract(paths.contracts, contract.model_copy(update={"status": ContractStatus.integrated}))
        commit_intent_files(paths, f"ateam: integrate {cid}", [saved])
    if db.contract_status(cid) != "integrated":
        db.transition("contract", cid, "integrated", by="integrate", reason=f"merged {merge}")
        echo(f"{cid} -> integrated ({merge[:12]})")
    return merge


def reopen_for_rebase(ctx, plan_id: str, cid: str, tip: str, echo) -> None:
    """Move a verified contract back to building for its rebase BUILD (owner, 2026-10-27): the contract file in an
    intent commit first, then the store, as merge_contract does. A repeat records only what is missing."""
    paths, db = ctx.paths, ctx.db
    contract = load_contracts(paths.contracts)[cid]
    if contract.status != ContractStatus.building:
        saved = save_contract(paths.contracts, contract.model_copy(update={"status": ContractStatus.building}))
        commit_intent_files(paths, f"ateam: rebase {cid} onto {integration_branch(plan_id)} {tip[:12]}", [saved])
    if db.contract_status(cid) != "building":
        db.transition("contract", cid, "building", by="run", reason=f"rebase onto {tip}")
        echo(f"{cid} -> building (rebase onto {integration_branch(plan_id)} {tip[:12]})")


def _rebase_under_way(db, owner: GitRepo, cid: str) -> bool:
    """The contract's worktree shows the rebase its key records, in the current attempt window."""
    from ateam.worktrees import worktree_path

    raw = db.get_meta(f"rebase:{cid}")
    if raw is None:
        return False
    rebase = json.loads(raw)
    if rebase["window"] != db.window_start(cid):
        return False
    try:
        wt = GitRepo(worktree_path(TargetPaths.from_root(owner.root), db, cid))
        return wt.merge_head() == rebase["tip"] or wt.is_ancestor(rebase["tip"], "HEAD")
    except GitError:
        return False


def reconcile_contracts(db, owner: GitRepo, cs, cids, branch: str, echo) -> None:
    """A contract whose file says integrated while the store says verified lost its store step in a crash, provided
    its verified head is on the integration branch; one whose file says building while the store says verified lost
    the store step of its rebase, provided the rebase is under way. Any other disagreement is refused."""
    for cid in cids:
        file_status, stored = str(cs[cid].status), db.contract_status(cid)
        if file_status == stored:
            continue
        head = db.latest_checkpoint(cid)["head"]
        if file_status == "integrated" and stored == "verified" and owner.branch_exists(branch) and owner.is_ancestor(head, branch):
            db.transition("contract", cid, "integrated", by="integrate", reason="reconciled after an interruption")
            echo(f"reconciled: {cid} -> integrated (state store)")
        elif file_status == "building" and stored == "verified" and _rebase_under_way(db, owner, cid):
            db.transition("contract", cid, "building", by="run", reason="reconciled after an interruption (rebase)")
            echo(f"reconciled: {cid} -> building (state store, rebase)")
        else:
            raise IntegrateRefused(f"{cid} is {file_status} in its file and {stored} in the state store; investigate before integrating")


def reconcile_feature(paths, db, plan: Plan, echo) -> str:
    """The store status after replaying the steps of integrate's own walk that a crash lost."""
    file_status, stored = str(plan.status), db.feature_status(plan.id)
    if file_status == stored:
        if stored not in WALK:
            raise IntegrateRefused(f"{plan.id} is {stored}; integrate needs an approved plan")
        return stored
    if stored == "merge_proposed" and file_status == "integrating":
        steps = ["integrating"]
    elif stored in WALK and file_status in WALK and WALK.index(file_status) > WALK.index(stored):
        steps = WALK[WALK.index(stored) + 1:WALK.index(file_status) + 1]
    else:
        raise IntegrateRefused(f"{plan.id} is {file_status} in its file and {stored} in the state store; investigate before integrating")
    if steps[-1] == "merge_proposed":
        try:
            intent_git(paths).show_file("HEAD", proposal_relpath(plan.id))
        except GitError:
            raise IntegrateRefused(f"{plan.id} is merge_proposed in its file but no proposal is committed") from None
    for s in steps:
        db.transition("feature", plan.id, s, by="integrate", reason="reconciled after an interruption")
    echo(f"reconciled: {plan.id} -> {steps[-1]} (state store)")
    return steps[-1]


def _prepare(paths, db, owner: GitRepo, config, plan_id: str, branch: str, target_tip: str, echo) -> GitRepo:
    """Record the start, then make or check the branch, then make or check the worktree."""
    key = f"integration:{plan_id}"
    raw = db.get_meta(key)
    if raw is None:
        start = target_tip
        db.set_meta(key, json.dumps({"branch": branch, "start": start, "target_branch": config.target_branch}))
    else:
        start = json.loads(raw)["start"]
    if not owner.branch_exists(branch):
        owner.create_branch(branch, start)
        echo(f"created {branch} at {start[:12]}")
    elif not owner.is_ancestor(start, owner.rev_parse(branch)):
        raise IntegrateRefused(f"{branch} does not descend from the recorded start {start[:12]}; investigate before integrating")
    path = integration_worktree(paths, plan_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            owner.worktree_add_existing(path, branch)
        except GitError as e:
            raise IntegrateRefused(f"could not create the integration worktree at {path}: {e}") from None
        echo(f"integration worktree: {path}")
    wt = GitRepo(path)
    try:
        current = wt.current_branch()
    except GitError:
        raise IntegrateRefused(f"{path} exists but is not a git worktree; move it aside") from None
    if current != branch:
        raise IntegrateRefused(f"the integration worktree {path} is on {current or 'a detached HEAD'}, not {branch}")
    if wt.merge_in_progress():
        raise IntegrateRefused(f"a merge is in progress in {path}; finish or abort it there first")
    dirty = wt.status_porcelain()
    if dirty:
        raise IntegrateRefused(f"the integration worktree {path} has uncommitted changes ({', '.join(p for _, p in dirty)})")
    return wt


def _pause(paths, db, plan: Plan, outcome, why: str, messages: list[str], echo, contracts: list[dict] = ()) -> IntegrateResult:
    set_plan_status(paths, db, plan, ["paused"], by="integrate", why=why)
    reason, _, detail = why.partition(": ")
    db.record_pause(plan.id, reason, detail, list(contracts), conflict=(outcome == "conflict"))
    echo(f"{plan.id} -> paused ({why})")
    return IntegrateResult(outcome, plan.id, messages=[why, *messages])


def _require_heads(db, git: GitRepo, cids: list[str], rev: str, branch: str) -> None:
    """Every contract's verified head is on `rev`: a recreated or reset branch never proposes without the work."""
    for cid in cids:
        head = db.latest_checkpoint(cid)["head"]
        if not git.is_ancestor(head, rev):
            raise IntegrateRefused(f"{cid} is integrated but its verified head {head[:12]} is not on {branch}; the branch "
                                   "was recreated or reset. Restore it, or reject the plan")


def _records(paths, plan: Plan, cs, cids) -> list[str]:
    recs = load_records(paths.records)
    status = lambda rid: str(recs[rid].status) if rid in recs else "missing"   # noqa: E731
    bound: dict[str, list[str]] = {}
    for cid in cids:
        for ref in cs[cid].records:
            bound.setdefault(ref.id, []).append(cid)
    return [*(f"{rid} {status(rid)} (proposed by {plan.id})" for rid in plan.proposed_records),
            *(f"{rid} {status(rid)} (bound by {', '.join(by)})" for rid, by in sorted(bound.items()))]


def _evidence(db, c: Contract) -> dict:
    cp = db.latest_checkpoint(c.id) or {}
    merged = [t["reason"] for t in db.transitions("contract", c.id) if (t.get("reason") or "").startswith("merged ")]
    return {"id": c.id, "title": c.title, "merge": merged[-1].removeprefix("merged ") if merged else "recorded after an interruption",
            "checks": cp.get("checks", []), "warnings": cp.get("warnings", [])}


def _risks(db, plan: Plan, cs, cids, warnings: list[str]) -> list[str]:
    risks = list(plan.unknowns)
    for cid in cids:
        risks += [f"{cid} {a.id} ({a.check.type}) not checked: {a.statement}" for a in cs[cid].acceptance if a.check.type != "test"]
        risks += [f"{cid} verify warning: {w}" for w in (db.latest_checkpoint(cid) or {}).get("warnings", []) if w != v.VERIFIER]
    return risks + [f"final checks: {w}" for w in warnings]
