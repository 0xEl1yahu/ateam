"""A snapshot of a checkout, taken before and after an in-place investigation. Any difference is tamper."""

from __future__ import annotations

from dataclasses import dataclass

from ateam.gitops import GitRepo


@dataclass(frozen=True)
class CheckoutSnapshot:
    head: str
    porcelain: list[tuple[str, str]]
    stashes: int


def take(git: GitRepo) -> CheckoutSnapshot:
    stashes = [line for line in git.run("stash", "list").splitlines() if line.strip()]
    return CheckoutSnapshot(head=git.head(), porcelain=git.status_porcelain(), stashes=len(stashes))


def changed(before: CheckoutSnapshot, after: CheckoutSnapshot) -> list[str]:
    """Human-readable differences; empty when the checkout is exactly as it was."""
    out: list[str] = []
    if before.head != after.head:
        out.append(f"HEAD moved from {before.head[:7]} to {after.head[:7]}")
    b, a = set(before.porcelain), set(after.porcelain)
    for xy, path in sorted(a - b):
        out.append(f"{path} is now {xy.strip() or 'changed'}")
    for xy, path in sorted(b - a):
        out.append(f"{path} no longer {xy.strip() or 'changed'}")
    if before.stashes != after.stashes:
        out.append(f"stash count changed from {before.stashes} to {after.stashes}")
    return out
