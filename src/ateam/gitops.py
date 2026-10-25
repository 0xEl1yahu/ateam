"""Thin wrapper over the git command line. No porcelain output is ever stripped."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


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
        prefixes = tuple(p.rstrip("/") + "/" for p in paths) + tuple(p.rstrip("/") for p in paths)
        return not any(path.startswith(prefixes) or path in paths for _, path in self.status_porcelain())

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

    def worktree_add(self, path: Path, branch: str, base: str) -> None:
        self.run("worktree", "add", "-q", "-b", branch, str(path), base)

    def worktree_remove(self, path: Path, force: bool = False) -> None:
        args = ["worktree", "remove", *(["--force"] if force else []), str(path)]
        self.run(*args)

    def exclude_in_worktree(self, path: Path, patterns: list[str]) -> None:
        """Add ignore patterns that apply inside `path`.

        Git reads info/exclude from the common dir, shared by every worktree,
        so the patterns are written there once (idempotently).
        """
        common = Path(_run(["git", "rev-parse", "--git-common-dir"], cwd=path).strip())
        if not common.is_absolute():
            common = (path / common).resolve()
        info = common / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        existing = exclude.read_text().splitlines() if exclude.is_file() else []
        missing = [p for p in patterns if p not in existing]
        if missing:
            with exclude.open("a") as fh:
                fh.write("".join(f"{p}\n" for p in missing))

def _run(cmd: list[str], cwd: Path) -> str:
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if r.returncode != 0:
        raise GitError(f"{' '.join(cmd)} failed in {cwd}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout
