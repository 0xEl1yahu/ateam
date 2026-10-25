from ateam.gitops import GitRepo
from ateam.runner.snapshot import CheckoutSnapshot, changed, take


def test_take_and_unchanged(target_repo):
    g = GitRepo(target_repo)
    before = take(g)
    assert isinstance(before, CheckoutSnapshot) and before.head == g.head() and before.porcelain == [] and before.stashes == 0
    assert changed(before, take(g)) == []


def test_changed_reports_head_paths_and_stash(target_repo):
    g = GitRepo(target_repo)
    before = take(g)
    (target_repo / "notes.txt").write_text("x\n")
    diff = changed(before, take(g))
    assert any("notes.txt" in d for d in diff)
    g.run("add", "notes.txt"); g.run("commit", "-q", "-m", "n")
    diff = changed(before, take(g))
    assert any("HEAD" in d for d in diff)
    (target_repo / "src/app.py").write_text("changed\n")
    g.run("stash", "push", "-q", "-m", "ateam-test-snapshot")
    diff = changed(before, take(g))
    assert any("stash" in d.lower() for d in diff)
    g.run("stash", "drop", "-q")



def test_changed_sees_edits_to_already_dirty_files_and_new_refs(target_repo):
    g = GitRepo(target_repo)
    (target_repo / "src/app.py").write_text("dirty before\n")
    (target_repo / "scratch.txt").write_text("untracked before\n")
    before = take(g)
    assert changed(before, take(g)) == []
    (target_repo / "src/app.py").write_text("dirty after\n")
    assert any("src/app.py" in d and "content" in d for d in changed(before, take(g)))
    (target_repo / "src/app.py").write_text("dirty before\n")
    (target_repo / "scratch.txt").write_text("untracked after\n")
    assert any("scratch.txt" in d for d in changed(before, take(g)))
    (target_repo / "scratch.txt").write_text("untracked before\n")
    g.run("branch", "side")
    assert any("ref" in d.lower() for d in changed(before, take(g)))
