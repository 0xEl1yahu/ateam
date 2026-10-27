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
    assert not g.is_clean() and not g.is_clean(["src"]) and g.is_clean(["tests"])


def test_commit_paths_only_stages_given_paths(target_repo):
    g = GitRepo(target_repo)
    (target_repo / "docs").mkdir(); (target_repo / "docs/x.md").write_text("x\n")
    (target_repo / "src/app.py").write_text("changed\n")
    sha = g.commit_paths(["docs"], "ateam: test")
    assert len(sha) == 40 and g.run("log", "-1", "--format=%s").strip() == "ateam: test"
    assert not g.is_clean(["src"]) and g.is_clean(["docs"])
    with pytest.raises(GitError):
        g.commit_paths(["docs"], "nothing to commit")


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
    assert g.is_ignored("src/__pycache__/x.pyc") and not g.is_ignored("src/app.py")


def test_is_clean_prefix_does_not_match_sibling_names(target_repo):
    g = GitRepo(target_repo)
    (target_repo / "docs-extra").mkdir(); (target_repo / "docs-extra/h.md").write_text("x\n")
    (target_repo / "docsrc").write_text("x\n")
    assert g.is_clean(["docs"]) and g.is_clean(["docs/"])
    (target_repo / "docs").mkdir(); (target_repo / "docs/new.md").write_text("x\n")
    assert not g.is_clean(["docs"])


# ---- primitives for integrate and merge

def _commit(g, rel, text, msg):
    (g.root / rel).parent.mkdir(parents=True, exist_ok=True)
    (g.root / rel).write_text(text); g.run("add", "-A"); g.run("commit", "-q", "-m", msg)
    return g.head()


def test_rev_parse_branch_exists_create_branch(target_repo):
    g = GitRepo(target_repo)
    head = g.head()
    assert g.rev_parse("main") == head and len(head) == 40
    with pytest.raises(GitError):
        g.rev_parse("nope")
    assert not g.branch_exists("ateam/P-0001")
    g.create_branch("ateam/P-0001", head)
    assert g.branch_exists("ateam/P-0001") and g.current_branch() == "main" and g.head() == head


def test_is_ancestor(target_repo):
    g = GitRepo(target_repo)
    first = g.head()
    second = _commit(g, "a.txt", "a\n", "a")
    assert g.is_ancestor(first, second) and g.is_ancestor(second, second) and not g.is_ancestor(second, first)
    with pytest.raises(GitError):
        g.is_ancestor("0" * 40, second)


def test_merge_no_ff_makes_a_merge_commit(target_repo, tmp_path):
    g = GitRepo(target_repo)
    base = g.head()
    g.create_branch("side", base)
    wt = tmp_path / "side"; g.run("worktree", "add", "-q", str(wt), "side")
    side = _commit(GitRepo(wt), "b.txt", "b\n", "b")
    new = g.merge_no_ff(side, "ateam: merge side")
    assert new == g.head() and g.run("rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [base, side]
    assert g.run("log", "-1", "--format=%s").strip() == "ateam: merge side"


def test_merge_conflict_aborts_and_names_paths(target_repo, tmp_path):
    from ateam.gitops import MergeConflict
    g = GitRepo(target_repo)
    base = g.head()
    g.create_branch("side", base)
    wt = tmp_path / "side"; g.run("worktree", "add", "-q", str(wt), "side")
    side = _commit(GitRepo(wt), "src/app.py", "side\n", "side")
    mine = _commit(g, "src/app.py", "mine\n", "mine")
    with pytest.raises(MergeConflict) as e:
        g.merge_no_ff(side, "ateam: merge side")
    assert e.value.paths == ["src/app.py"]
    assert g.head() == mine and g.status_porcelain() == [] and not g.merge_in_progress()


def test_merge_ff_only(target_repo, tmp_path):
    g = GitRepo(target_repo)
    base = g.head()
    g.create_branch("ahead", base)
    wt = tmp_path / "ahead"; g.run("worktree", "add", "-q", str(wt), "ahead")
    ahead = _commit(GitRepo(wt), "c.txt", "c\n", "c")
    assert g.merge_ff_only(ahead) == ahead == g.head()
    _commit(GitRepo(wt), "d.txt", "d\n", "d")
    mine = _commit(g, "e.txt", "e\n", "e")
    with pytest.raises(GitError):
        g.merge_ff_only(GitRepo(wt).head())
    assert g.head() == mine


def test_worktree_add_existing_and_show_file(target_repo, tmp_path):
    g = GitRepo(target_repo)
    g.create_branch("ateam/P-0001", g.head())
    wt = tmp_path / "integ"
    g.worktree_add_existing(wt, "ateam/P-0001")
    assert GitRepo(wt).current_branch() == "ateam/P-0001"
    assert g.show_file("HEAD", "src/app.py").startswith("def greet")
    with pytest.raises(GitError):
        g.show_file("HEAD", "missing.txt")


def test_files_at_lists_tracked_files_at_a_rev(target_repo):
    g = GitRepo(target_repo)
    before = set(g.files_at("HEAD"))
    rev = _commit(g, "a.txt", "a\n", "a")
    _commit(g, "b/c.txt", "c\n", "c")
    assert set(g.files_at(rev)) - before == {"a.txt"}
    assert set(g.files_at("HEAD")) - before == {"a.txt", "b/c.txt"}
    assert g.files_at("HEAD") == sorted(g.files_at("HEAD"))
