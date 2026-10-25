"""Verify the worktree itself, never the worker's account of it."""

from __future__ import annotations

from dataclasses import dataclass

from ateam.gitops import GitRepo
from ateam.globs import matches

DEFAULT_IGNORE: tuple[str, ...] = (".claude/**",)


@dataclass
class TreeState:
    base: str
    head: str
    dirty: list[tuple[str, str]]
    changed_vs_base: list[str]


@dataclass
class ScopeResult:
    ok: bool
    outside: list[str]
    dirty: list[str]


def tree_state(git: GitRepo, base: str) -> TreeState:
    return TreeState(base=base, head=git.head(), dirty=git.status_porcelain(), changed_vs_base=git.diff_names(base))


def scope_check(
    state: TreeState, allowed: list[str], incidental: list[str], ignore: tuple[str, ...] = DEFAULT_IGNORE
) -> ScopeResult:
    def ignored(p: str) -> bool:
        return any(matches(g, p) for g in ignore)

    def permitted(p: str) -> bool:
        return any(matches(g, p) for g in [*allowed, *incidental])

    dirty = sorted(p for _, p in state.dirty if not ignored(p))
    candidates = set(dirty) | {p for p in state.changed_vs_base if not ignored(p)}
    outside = sorted(p for p in candidates if not permitted(p))
    return ScopeResult(ok=not outside and not dirty, outside=outside, dirty=dirty)
