"""Contract worktrees: <ATeam home>/worktrees/<contract-id> on branch ateam/<contract-id> (owner, 2026-10-26).

A contract's worktree is cut from the integration-branch tip when the contract becomes ready (spec, "Verification and
integration" step 2). The tip is recorded as the contract's base before anything is created, so a re-run, or a run
after a crash between the two, cuts from the same commit and never from a newer tip. Worktrees are never removed.

A lane's contracts share one worktree, <home>/worktrees/<first contract id>, on branch ateam/<first contract id>
(design 2026-10-27, decision 2): a later contract's base is its predecessor's verified head, so its own diff is what
scope, tamper and TEST judge. Each contract's lane head (`lane:<cid>`) and base are recorded before anything is
created and win over any later call, so a re-run never moves a contract.
"""

from __future__ import annotations

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
    the first call records its lane head and its verified head as this contract's base."""
    key = f"base:{cid}"
    base, head = db.get_meta(key), db.get_meta(f"lane:{cid}")
    if head is None:
        head = (db.get_meta(f"lane:{after}") or after) if after is not None and base is None else cid
        db.set_meta(f"lane:{cid}", head)   # a contract cut before lanes (a base, no lane) stays its own lane head
    if base is None:
        base = db.latest_checkpoint(after)["head"] if after is not None else owner.rev_parse(f"ateam/{plan_id}")
        db.set_meta(key, base)
    branch = f"ateam/{head}"
    path = home_dir(paths.root) / "worktrees" / head
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if owner.branch_exists(branch):
            if not owner.is_ancestor(base, owner.rev_parse(branch)):
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
    if not wt.is_ancestor(base, "HEAD") and wt.merge_head() != base:   # merge_head: a rebase onto base is under way
        raise WorktreeError(f"the worktree {path} does not descend from {cid}'s recorded base {base[:12]}")
    return ContractWorktree(cid, path, base, branch)
