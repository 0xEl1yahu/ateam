"""Contract worktrees: <ATeam home>/worktrees/<contract-id> on branch ateam/<contract-id> (owner, 2026-10-26).

A contract's worktree is cut from the integration-branch tip when the contract becomes ready (spec, "Verification and
integration" step 2). The tip is recorded as the contract's base before anything is created, so a re-run, or a run
after a crash between the two, cuts from the same commit and never from a newer tip. Worktrees are never removed.
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


def contract_worktree(paths: TargetPaths, db: StateDB, owner: GitRepo, plan_id: str, cid: str) -> ContractWorktree:
    key = f"base:{cid}"
    base = db.get_meta(key)
    if base is None:
        base = owner.rev_parse(f"ateam/{plan_id}")
        db.set_meta(key, base)
    branch = f"ateam/{cid}"
    path = home_dir(paths.root) / "worktrees" / cid
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
    if not wt.is_ancestor(base, "HEAD"):
        raise WorktreeError(f"the worktree {path} does not descend from {cid}'s recorded base {base[:12]}")
    return ContractWorktree(cid, path, base, branch)
