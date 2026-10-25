import pytest

from ateam.gitops import GitError, GitRepo


def test_discover_branch_head_clean(target_repo):
    g = GitRepo.discover(target_repo / "src")
    assert g.root == target_repo
    assert g.current_branch() == "main"
    assert len(g.head()) == 40
    assert g.is_clean()


def test_porcelain_keeps_leading_space(target_repo):
    (target_repo / "src/app.py").write_text("changed\n")
    (target_repo / "new.txt").write_text("x\n")
    st = GitRepo(target_repo).status_porcelain()
    assert (" M", "src/app.py") in st and ("??", "new.txt") in st


def test_is_clean_scoped_to_paths(target_repo):
    (target_repo / "src/app.py").write_text("changed\n")
    g = GitRepo(target_repo)
    assert not g.is_clean() and not g.is_clean(["src"]) and g.is_clean([".ateam"])


def test_commit_paths_only_stages_given_paths(target_repo):
    g = GitRepo(target_repo)
    (target_repo / ".ateam/records/x.yaml").write_text("id: x\n")
    (target_repo / "src/app.py").write_text("changed\n")
    sha = g.commit_paths([".ateam/records"], "ateam: test")
    assert len(sha) == 40 and g.run("log", "-1", "--format=%s").strip() == "ateam: test"
    assert not g.is_clean(["src"]) and g.is_clean([".ateam"])
    with pytest.raises(GitError):
        g.commit_paths([".ateam/records"], "nothing to commit")


def test_diff_names(target_repo):
    g = GitRepo(target_repo); base = g.head()
    (target_repo / "src/app.py").write_text("changed\n"); g.run("add", "src/app.py"); g.run("commit", "-q", "-m", "c")
    assert g.diff_names(base) == ["src/app.py"]


def test_worktree_add_remove(target_repo, tmp_path):
    g = GitRepo(target_repo)
    wt = tmp_path / "wt"
    g.worktree_add(wt, "wt-test", "main")
    assert (wt / "src/app.py").is_file() and GitRepo(wt).current_branch() == "wt-test"
    assert GitRepo(wt).is_linked_worktree() and not g.is_linked_worktree()
    g.worktree_remove(wt, force=True)
    assert not wt.exists()


def test_is_ignored(target_repo):
    g = GitRepo(target_repo)
    assert g.is_ignored(".ateam-scratch/handoff.yaml") and not g.is_ignored("src/app.py")


def test_is_clean_prefix_does_not_match_sibling_names(target_repo):
    g = GitRepo(target_repo)
    (target_repo / ".ateam-scratch").mkdir(); (target_repo / ".ateam-scratch/h.yaml").write_text("x\n")
    (target_repo / ".ateamrc").write_text("x\n")
    assert g.is_clean([".ateam"]) and g.is_clean([".ateam/"])
    (target_repo / ".ateam/records/new.yaml").write_text("x\n")
    assert not g.is_clean([".ateam"])
