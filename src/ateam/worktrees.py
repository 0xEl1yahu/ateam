"""Contract worktrees: <ATeam home>/worktrees/<contract-id> on branch ateam/<contract-id> (owner, 2026-10-26).

A contract's worktree is cut from the integration-branch tip when the contract becomes ready (spec, "Verification and
integration" step 2). The tip is recorded as the contract's base before anything is created, so a re-run, or a run
after a crash between the two, cuts from the same commit and never from a newer tip. Worktrees are never removed.

A lane's contracts share one worktree, <home>/worktrees/<first contract id>, on branch ateam/<first contract id>
(design 2026-10-27, decision 2): a later contract's base is the integration tip at its first call (owner decision 7,
2026-10-28), and the lane branch is fast-forwarded to it, so its own diff is what scope, tamper and TEST judge.
Each contract's lane head (`lane:<cid>`) and base are recorded before anything is created and win over any later call, so a re-run never moves a contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ateam.gitops import GitError, GitRepo
from ateam.paths import TargetPaths, home_dir
from ateam.state.db import StateDB


class WorktreeError(Exception):
    pass


@dataclass(frozen=True)
class ContractWorktree:
    cid: str
    path: Path
    base: str
    branch: str


def worktree_path(paths: TargetPaths, db: StateDB, cid: str) -> Path:
    """Where a contract's work is: its lane's worktree."""
    return home_dir(paths.root) / "worktrees" / (db.get_meta(f"lane:{cid}") or cid)


def contract_worktree(paths: TargetPaths, db: StateDB, owner: GitRepo, plan_id: str, cid: str,
                      after: str | None = None) -> ContractWorktree:
    """The contract's worktree, made if missing. `after` is the contract before it in its lane (already integrated):
    the first call records its lane head and the integration tip as this contract's base, then the lane branch is
    fast-forwarded to it (only when no merge is under way and HEAD is a strict ancestor of the base)."""
    key = f"base:{cid}"
    base, head = db.get_meta(key), db.get_meta(f"lane:{cid}")
    if head is None:
        head = (db.get_meta(f"lane:{after}") or after) if after is not None and base is None else cid
        db.set_meta(f"lane:{cid}", head)   # a contract cut before lanes (a base, no lane) stays its own lane head
    if base is None:
        base = owner.rev_parse(f"ateam/{plan_id}")
        db.set_meta(key, base)
    branch = f"ateam/{head}"
    path = home_dir(paths.root) / "worktrees" / head
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if owner.branch_exists(branch):
            tip = owner.rev_parse(branch)
            if not owner.is_ancestor(base, tip) and not (after is not None and owner.is_ancestor(tip, base)):
                raise WorktreeError(f"{branch} does not descend from {cid}'s recorded base {base[:12]}")
            owner.worktree_add_existing(path, branch)
        else:
            owner.worktree_add(path, branch, base)
    wt = GitRepo(path)
    try:
        current = wt.current_branch()
        linked = wt.is_linked_worktree()
    except GitError:
        current, linked = None, False
    if not linked:
        raise WorktreeError(f"{path} exists but is not a worktree; move it aside")
    if current != branch:
        raise WorktreeError(f"the worktree {path} is on {current or 'a detached HEAD'}, not {branch}")
    if (after is not None and not wt.merge_in_progress() and not wt.is_ancestor(base, "HEAD")
            and wt.is_ancestor("HEAD", base)):
        left = [p for _, p in wt.status_porcelain()]
        if left:
            raise WorktreeError(f"the lane worktree has files left by an earlier contract ({', '.join(left)})")
        try:
            wt.merge_ff_only(base)
        except GitError as e:
            raise WorktreeError(f"{cid}: {branch} cannot fast-forward to the integration tip {base[:12]}: {e}") from e
    if not wt.is_ancestor(base, "HEAD") and wt.merge_head() != base:   # merge_head: a rebase onto base is under way
        raise WorktreeError(f"the worktree {path} does not descend from {cid}'s recorded base {base[:12]}")
    return ContractWorktree(cid, path, base, branch)


def rebased_this_window(db: StateDB, cid: str) -> bool:
    """A rebase was prepared in the contract's current attempt window and a counted BUILD has run since: a crash
    between preparing it and its BUILD does not use it up."""
    raw = db.get_meta(f"rebase:{cid}")
    if raw is None:
        return False
    rebase = json.loads(raw)
    if rebase["window"] != db.window_start(cid):
        return False
    return any(a["attempt_id"] > rebase["after"] and a.get("outcome") != "interrupted" for a in db.attempts_for(cid, role="build"))


def prepare_rebase(paths: TargetPaths, db: StateDB, owner: GitRepo, plan_id: str, cid: str,
                   tip: str) -> tuple[ContractWorktree, list[str]]:
    """Merge the integration tip into the contract's own worktree, without committing, for the rebase BUILD (owner,
    2026-10-27): workers may not run git merge, so the scheduler starts it and the BUILD concludes it. Idempotent:
    an open merge onto the recorded tip is kept, and a tip HEAD already holds is not merged again. The base moves to the tip only once
    the merge is under way, so a merge that fails leaves the contract as it was. The rebase:<cid> meta gains the
    merge's conflicting paths ('paths') once it is under way; they survive staging, which the unmerged paths do not."""
    wt = GitRepo(worktree_path(paths, db, cid))
    raw = db.get_meta(f"rebase:{cid}")
    meta = json.loads(raw) if raw else {}
    recorded = meta.get("tip")
    if recorded is not None and wt.merge_head() == recorded:
        if "paths" not in meta:   # a crash after the merge began, or meta written before 'paths' was recorded
            db.set_meta(f"rebase:{cid}", json.dumps({**meta, "paths": wt.unmerged_paths()}))
        if db.get_meta(f"base:{cid}") != recorded:
            db.set_meta(f"base:{cid}", recorded)
        return contract_worktree(paths, db, owner, plan_id, cid), wt.unmerged_paths()
    if not wt.merge_in_progress() and wt.is_ancestor(tip, "HEAD"):   # the tip is already in: nothing to merge
        return contract_worktree(paths, db, owner, plan_id, cid), []
    after = max((a["attempt_id"] for a in db.attempts_for(cid)), default=0)
    meta = {"window": db.window_start(cid), "tip": tip, "after": after}
    db.set_meta(f"rebase:{cid}", json.dumps(meta))
    conflicts = wt.merge_keep_conflicts(tip)
    if wt.merge_head() == tip:   # the conflicting paths, kept so the scheduler can check them once they are staged
        db.set_meta(f"rebase:{cid}", json.dumps({**meta, "paths": conflicts}))
        db.set_meta(f"base:{cid}", tip)
    return contract_worktree(paths, db, owner, plan_id, cid), conflicts
