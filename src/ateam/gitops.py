"""Thin wrapper over the git command line. No porcelain output is ever stripped."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


class MergeConflict(GitError):
    """A merge stopped on conflicts; it has been aborted, so the checkout is as it was."""

    def __init__(self, paths: list[str], message: str) -> None:
        super().__init__(message)
        self.paths = paths


class GitRepo:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @classmethod
    def discover(cls, start: Path) -> GitRepo:
        out = _run(["git", "rev-parse", "--show-toplevel"], cwd=start)
        return cls(Path(out.strip()))

    def run(self, *args: str) -> str:
        return _run(["git", *args], cwd=self.root)

    def current_branch(self) -> str:
        return self.run("branch", "--show-current").strip()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def status_porcelain(self) -> list[tuple[str, str]]:
        out = self.run("status", "--porcelain", "--untracked-files=all", "-z")
        entries: list[tuple[str, str]] = []
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            item = parts[i]
            if not item:
                i += 1
                continue
            xy, path = item[:2], item[3:]
            entries.append((xy, path))
            if xy[0] in "RC":  # renames carry the original path in the next field
                i += 1
            i += 1
        return entries

    def is_clean(self, paths: list[str] | None = None) -> bool:
        if not paths:
            return not self.status_porcelain()
        roots = [p.rstrip("/") for p in paths]
        return not any(
            path == r or path.startswith(r + "/") for _, path in self.status_porcelain() for r in roots
        )

    def diff_names(self, base: str) -> list[str]:
        out = self.run("diff", "--name-only", base)
        return [line for line in out.splitlines() if line]

    def commit_paths(self, paths: list[str], message: str) -> str:
        self.run("add", "-A", "--", *paths)
        staged = self.run("diff", "--cached", "--name-only").strip()
        if not staged:
            raise GitError(f"nothing to commit under {paths}")
        self.run("commit", "-q", "-m", message, "--only", "--", *paths)
        return self.head()

    def is_linked_worktree(self) -> bool:
        """True when this checkout is a linked worktree (its git dir differs from the common dir)."""
        git_dir = Path(self.run("rev-parse", "--git-dir").strip())
        common = Path(self.run("rev-parse", "--git-common-dir").strip())
        return (self.root / git_dir).resolve() != (self.root / common).resolve()

    def worktree_add(self, path: Path, branch: str, base: str) -> None:
        self.run("worktree", "add", "-q", "-b", branch, str(path), base)

    def worktree_remove(self, path: Path, force: bool = False) -> None:
        args = ["worktree", "remove", *(["--force"] if force else []), str(path)]
        self.run(*args)

    def rev_parse(self, ref: str) -> str:
        """The full sha of the commit `ref` names; GitError when it names none."""
        return self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()

    def branch_exists(self, name: str) -> bool:
        r = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=self.root)
        return r.returncode == 0

    def create_branch(self, name: str, start: str) -> None:
        """Make a branch at `start` without checking it out."""
        self.run("branch", name, start)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True when `ancestor` is reachable from `descendant` (a commit is its own ancestor)."""
        r = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=self.root, text=True, capture_output=True)
        if r.returncode in (0, 1):
            return r.returncode == 0
        raise GitError(f"git merge-base --is-ancestor {ancestor} {descendant} failed in {self.root}: {r.stderr.strip()}")

    def files_at(self, rev: str) -> list[str]:
        """Tracked file paths at `rev`, sorted (NUL-separated, so no path comes back quoted)."""
        return sorted(p for p in self.run("ls-tree", "-r", "-z", "--name-only", rev).split("\0") if p)

    def merge_in_progress(self) -> bool:
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"], cwd=self.root, capture_output=True)
        return r.returncode == 0

    def merge_head(self) -> str | None:
        """The commit being merged while a merge is in progress, else None."""
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"], cwd=self.root, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def merge_no_ff(self, rev: str, message: str) -> str:
        """Merge `rev` with a merge commit and return the new HEAD. Any failure that leaves a merge in progress is
        aborted first; a conflict raises MergeConflict naming the conflicting paths."""
        try:
            self.run("merge", "--no-ff", "--no-edit", "-m", message, rev)
        except GitError as e:
            if not self.merge_in_progress():
                raise
            paths = [p for p in self.run("diff", "--name-only", "--diff-filter=U").splitlines() if p]
            self.run("merge", "--abort")
            if paths:
                raise MergeConflict(paths, f"merging {rev} conflicts in {', '.join(paths)}") from e
            raise
        return self.head()

    def merge_ff_only(self, rev: str) -> str:
        """Fast-forward to `rev` and return the new HEAD; GitError (nothing changed) when that is not a fast-forward."""
        self.run("merge", "--ff-only", "-q", rev)
        return self.head()

    def worktree_add_existing(self, path: Path, branch: str) -> None:
        """Check out an existing branch in a new linked worktree."""
        self.run("worktree", "add", "-q", str(path), branch)

    def show_file(self, rev: str, relpath: str) -> str:
        """The content of `relpath` as committed at `rev`."""
        return self.run("show", f"{rev}:{relpath}")

    def diff_stat(self, a: str, b: str) -> str:
        return self.run("diff", "--stat", a, b)

    def is_ignored(self, path: str) -> bool:
        """True when git would ignore `path` (tracked .gitignore rules, so the same on every clone)."""
        r = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=self.root, text=True, capture_output=True)
        return r.returncode == 0

def _run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if r.returncode != 0:
        raise GitError(f"{' '.join(cmd)} failed in {cwd}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout
