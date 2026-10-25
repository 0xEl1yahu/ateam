"""The private intent repo: a git repository per target under ATeam's home.

It holds records, contracts, plans, reports and config. It can be pushed to a private
remote so another machine, pointed at a clone of the same target, pulls the same intent.
The target repository never contains any of this.
"""

from __future__ import annotations

from ateam.config import TargetConfig, save_config
from ateam.gitops import GitError, GitRepo
from ateam.paths import TargetPaths

FIRST_COMMIT = "ateam: initialise intent repo"


class IntentError(Exception):
    pass


def intent_git(paths: TargetPaths) -> GitRepo:
    if not paths.exists():
        raise IntentError(f"no intent repo for {paths.root} at {paths.intent}; run: ateam intent init")
    return GitRepo(paths.intent)


def init_intent(paths: TargetPaths, remote: str | None = None) -> GitRepo:
    """Create the intent repo (or adopt an existing one). With a remote, fetch its main first."""
    intent = paths.intent
    if intent.exists():
        if not (intent / ".git").exists():
            raise IntentError(f"{intent} exists but is not a git repository; move it aside and run ateam intent init again")
        git = GitRepo(intent)
        if remote:
            set_remote(paths, remote)
        return git
    intent.mkdir(parents=True)
    git = GitRepo(intent)
    git.run("init", "-q", "-b", "main")
    _ensure_identity(git)
    if remote:
        git.run("remote", "add", "origin", remote)
        try:
            git.run("fetch", "-q", "origin", "main")
            git.run("reset", "-q", "--hard", "FETCH_HEAD")
            git.run("branch", "-q", "--set-upstream-to=origin/main", "main")
        except GitError:
            pass  # empty or unreachable remote: scaffold locally, push later
    if not paths.config_file.exists():
        for d in (paths.records, paths.contracts, paths.plans, paths.reports):
            d.mkdir(parents=True, exist_ok=True)
            (d / ".gitkeep").write_text("")
        save_config(paths, TargetConfig())
        git.run("add", "-A")
        git.run("commit", "-q", "-m", FIRST_COMMIT)
    return git


def set_remote(paths: TargetPaths, url: str) -> None:
    git = intent_git(paths)
    if "origin" in git.run("remote").split():
        git.run("remote", "set-url", "origin", url)
    else:
        git.run("remote", "add", "origin", url)


def push(paths: TargetPaths) -> str:
    return intent_git(paths).run("push", "-u", "origin", "main")


def pull(paths: TargetPaths) -> str:
    return intent_git(paths).run("pull", "--ff-only", "origin", "main")


def commit_intent(paths: TargetPaths, message: str, subpaths: list[str]) -> str:
    """Stage only `subpaths` (relative to the intent repo) and commit them."""
    git = intent_git(paths)
    try:
        return git.commit_paths(subpaths, message)
    except GitError as e:
        if "nothing to commit" in str(e):
            raise IntentError(f"nothing to commit under {subpaths} in {paths.intent}") from e
        raise


def _ensure_identity(git: GitRepo) -> None:
    """Commits need an identity; fall back to a local one when none is configured."""
    for key, value in (("user.name", "ateam"), ("user.email", "ateam@localhost")):
        try:
            if not git.run("config", key).strip():
                raise GitError("unset")
        except GitError:
            git.run("config", key, value)
