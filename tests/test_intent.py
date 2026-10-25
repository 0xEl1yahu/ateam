import subprocess

import pytest

from ateam.config import load_config
from ateam.gitops import GitRepo
from ateam.intent import IntentError, commit_intent, init_intent, intent_git, pull, push, set_remote
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
