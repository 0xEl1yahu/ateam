"""A snapshot of a checkout, taken before and after an in-place investigation. Any difference is tamper.

Covers HEAD, every ref, the stash count, the set of dirty and untracked paths, and the content of each
of those paths (so an edit to a file that was already dirty is seen). It cannot see writes under
gitignored paths, outside the checkout, or to the state store; the policy pre-guards cover those.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ateam.gitops import GitRepo


@dataclass(frozen=True)
class CheckoutSnapshot:
    head: str
    porcelain: list[tuple[str, str]]
    stashes: int
    refs: str = ""
    dirty_hashes: dict[str, str] = field(default_factory=dict)


def _hash_file(git: GitRepo, path: str) -> str | None:
    p = git.root / path
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def take(git: GitRepo) -> CheckoutSnapshot:
    stashes = [line for line in git.run("stash", "list").splitlines() if line.strip()]
    porcelain = git.status_porcelain()
    refs = hashlib.sha256(git.run("for-each-ref", "--format=%(objectname) %(refname)").encode()).hexdigest()
    hashes = {path: h for _, path in porcelain if (h := _hash_file(git, path)) is not None}
    return CheckoutSnapshot(head=git.head(), porcelain=porcelain, stashes=len(stashes), refs=refs, dirty_hashes=hashes)


def changed(before: CheckoutSnapshot, after: CheckoutSnapshot) -> list[str]:
    """Human-readable differences; empty when the checkout is exactly as it was."""
    out: list[str] = []
    if before.head != after.head:
        out.append(f"HEAD moved from {before.head[:7]} to {after.head[:7]}")
    if before.refs != after.refs:
        out.append("refs changed (a branch or tag was created, moved or deleted)")
    b, a = set(before.porcelain), set(after.porcelain)
    for xy, path in sorted(a - b):
        out.append(f"{path} is now {xy.strip() or 'changed'}")
    for xy, path in sorted(b - a):
        out.append(f"{path} no longer {xy.strip() or 'changed'}")
    for path, h in sorted(after.dirty_hashes.items()):
        if path in before.dirty_hashes and before.dirty_hashes[path] != h:
            out.append(f"{path} content changed (it was already dirty before the session)")
    if before.stashes != after.stashes:
        out.append(f"stash count changed from {before.stashes} to {after.stashes}")
    return out
