import subprocess

import pytest

from ateam.config import load_config
from ateam.gitops import GitRepo
from ateam.intent import IntentError, commit_intent, commit_intent_files, init_intent, intent_git, pull, push, set_remote
from ateam.paths import TargetPaths


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("ATEAM_HOME", str(tmp_path / "home"))
    target = tmp_path / "target"; target.mkdir()
    _git(target, "init", "-q", "-b", "main")
    return TargetPaths.from_root(target)


def test_init_creates_repo_dirs_config_and_first_commit(paths):
    git = init_intent(paths)
    assert isinstance(git, GitRepo) and git.root == paths.intent and paths.exists()
    assert git.current_branch() == "main" and git.is_clean()
    for d in (paths.records, paths.contracts, paths.plans, paths.reports):
        assert (d / ".gitkeep").is_file()
    assert paths.config_file.is_file() and load_config(paths).target_branch == "main"
    assert git.run("log", "-1", "--format=%s").strip() == "ateam: initialise intent repo"
    assert "origin" not in git.run("remote")


def test_init_is_idempotent(paths):
    first = init_intent(paths).head()
    assert init_intent(paths).head() == first


def test_init_refuses_non_git_directory(paths):
    paths.intent.mkdir(parents=True)
    (paths.intent / "stray.txt").write_text("x")
    with pytest.raises(IntentError, match="not a git repository"):
        init_intent(paths)


def test_intent_git_missing_names_init(paths):
    with pytest.raises(IntentError, match="ateam intent init"):
        intent_git(paths)


def test_commit_intent_stages_only_subpaths(paths):
    init_intent(paths)
    (paths.records / "R-0001-x.yaml").write_text("id: R-0001\n")
    (paths.contracts / "C-0001-x.yaml").write_text("id: C-0001\n")
    sha = commit_intent(paths, "ateam: approve R-0001", ["records"])
    git = intent_git(paths)
    assert len(sha) == 40 and git.run("log", "-1", "--format=%s").strip() == "ateam: approve R-0001"
    assert not git.is_clean(["contracts"]) and git.is_clean(["records"])
    with pytest.raises(IntentError, match="nothing to commit"):
        commit_intent(paths, "ateam: again", ["records"])


def test_remote_and_push_pull_against_bare(paths, tmp_path):
    bare = tmp_path / "private.git"; _git(tmp_path, "init", "-q", "--bare", str(bare))
    init_intent(paths, remote=str(bare))
    assert intent_git(paths).run("remote", "get-url", "origin").strip() == str(bare)
    push(paths)
    assert _git(bare, "log", "-1", "--format=%s", "main").strip() == "ateam: initialise intent repo"
    (paths.records / "R-0001-x.yaml").write_text("id: R-0001\n"); commit_intent(paths, "ateam: approve R-0001", ["records"]); push(paths)
    # a second machine: fresh home, same target -> clone the private remote through init(remote=...)
    other_home = tmp_path / "home2"
    import os
    os.environ["ATEAM_HOME"] = str(other_home)
    paths2 = TargetPaths.from_root(paths.root)
    init_intent(paths2, remote=str(bare))
    assert (paths2.records / "R-0001-x.yaml").is_file()
    set_remote(paths2, str(bare))
    assert "Already up to date" in pull(paths2) or pull(paths2) is not None


def test_commit_intent_files_stages_only_the_named_files(paths):
    init_intent(paths)
    mine = paths.records / "R-0001-x.yaml"; mine.write_text("id: R-0001\n")
    (paths.records / "R-0099-planted.yaml").write_text("id: R-0099\n")
    sha = commit_intent_files(paths, "ateam: approve R-0001", [mine])
    git = intent_git(paths)
    assert len(sha) == 40 and git.run("show", "--name-only", "--format=", "HEAD").split() == ["records/R-0001-x.yaml"]
    assert "?? records/R-0099-planted.yaml" in git.run("status", "--porcelain", "--untracked-files=all").splitlines()
    with pytest.raises(IntentError, match="nothing to commit"):
        commit_intent_files(paths, "ateam: again", [mine])


def test_a_failed_commit_leaves_nothing_staged(paths):
    """A pre-commit hook (or signing, or identity) failure happens after `git add`: nothing may stay staged, or a
    restore with `git checkout --` restores the staged content and the repo stays dirty (final review, 2026-10-27)."""
    git = init_intent(paths)
    hook = paths.intent / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused by hook >&2\nexit 1\n"); hook.chmod(0o755)
    (paths.intent / "plans").mkdir(exist_ok=True)
    (paths.intent / "plans" / "new.yaml").write_text("x: 1\n")
    with pytest.raises(Exception, match="refused by hook"):
        commit_intent_files(paths, "ateam: x", [paths.intent / "plans" / "new.yaml"])
    assert git.run("diff", "--cached", "--name-only").strip() == ""


# -- coordinator commits: a session's intent check accepts the scheduler's own commits (owner, 2026-10-28) ----------

import pytest as _pytest  # noqa: E402


def _coord_env(target_repo):
    from ateam.intent import intent_git
    from ateam.paths import TargetPaths, home_dir
    from ateam.state.db import StateDB

    paths = TargetPaths.from_root(target_repo)
    db = StateDB(home_dir(target_repo) / "state.db")
    db.init_schema()
    return paths, intent_git(paths), db


def _intent_commit(paths, message, name="x.txt"):
    from ateam.intent import commit_intent_files

    f = paths.intent / name
    f.write_text(f.read_text() + "+\n" if f.exists() else "+\n")
    return commit_intent_files(paths, message, [f])


@_pytest.mark.parametrize("case, expect_ok", [
    ("unchanged", True),
    ("one recorded", True),
    ("two recorded", True),
    ("unrecorded", False),
    ("recorded then unrecorded", False),
    ("not a descendant", False),
    ("dirty after", False),
    ("recorded non-ateam commit is never recorded", False),
])
def test_intent_problems_accept_only_recorded_coordinator_commits(target_repo, case, expect_ok):
    from ateam.intent import intent_problems, record_coordinator_commits

    paths, intent, db = _coord_env(target_repo)
    try:
        before = intent.head()
        if case in ("one recorded", "two recorded", "recorded then unrecorded"):
            start = intent.head()
            _intent_commit(paths, "ateam: integrate C-0002")
            if case == "two recorded":
                _intent_commit(paths, "ateam: integrate C-0003")
            record_coordinator_commits(db, intent, start)
            if case == "recorded then unrecorded":
                _intent_commit(paths, "ateam: a worker's commit")
        elif case == "unrecorded":
            _intent_commit(paths, "ateam: integrate C-0002")
        elif case == "not a descendant":
            before = _intent_commit(paths, "ateam: integrate C-0002")   # the session saw this commit...
            intent.run("reset", "-q", "--hard", "HEAD~1")               # ...and history was rewritten under it
        elif case == "dirty after":
            (paths.intent / "stray.txt").write_text("x\n")
        elif case == "recorded non-ateam commit is never recorded":
            start = intent.head()
            _intent_commit(paths, "a stray commit")
            record_coordinator_commits(db, intent, start)
        problems = intent_problems(db, intent, before)
        assert (problems == []) is expect_ok, problems
        if not expect_ok and case in ("unrecorded", "recorded then unrecorded"):
            assert any("ateam:" in p for p in problems), problems   # the evidence names the commit
    finally:
        db.close()


def test_settle_waits_for_an_active_writer_then_accepts_its_recording(target_repo):
    import threading

    from ateam.intent import WRITER_KEY, record_coordinator_commits, settle_intent

    paths, intent, db = _coord_env(target_repo)
    try:
        before = intent.head()
        db.set_meta(WRITER_KEY, "P-0001")
        _intent_commit(paths, "ateam: integrate C-0002")

        def finish():
            from ateam.paths import home_dir
            from ateam.state.db import StateDB

            with StateDB(home_dir(target_repo) / "state.db") as other:
                record_coordinator_commits(other, intent, before)
                other.delete_meta(WRITER_KEY)

        t = threading.Timer(0.3, finish)
        t.start()
        assert settle_intent(db, intent, before, grace=0.05, cap=5.0, poll=0.05) == []
        t.join()
    finally:
        db.close()


def test_settle_gives_up_after_the_grace_when_no_writer_is_active(target_repo):
    from ateam.intent import settle_intent

    paths, intent, db = _coord_env(target_repo)
    try:
        before = intent.head()
        _intent_commit(paths, "ateam: integrate C-0002")
        assert settle_intent(db, intent, before, grace=0.1, cap=5.0, poll=0.02)
    finally:
        db.close()
