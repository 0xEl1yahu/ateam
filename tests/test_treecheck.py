from ateam.gitops import GitRepo
from ateam.treecheck import scope_check, tree_state


def test_tree_state_and_scope(target_repo):
    g = GitRepo(target_repo)
    base = g.head()
    (target_repo / "src/api/x.py").write_text("changed\n")
    (target_repo / "notes").mkdir()
    (target_repo / "notes/out.txt").write_text("x\n")
    (target_repo / ".ateam-scratch").mkdir()
    (target_repo / ".ateam-scratch/handoff.yaml").write_text("progress: x\n")
    st = tree_state(g, base)
    assert st.base == base and st.head == base and st.changed_vs_base == ["src/api/x.py"]
    r = scope_check(st, allowed=["src/api/**"], incidental=[])
    assert r.outside == ["notes/out.txt"]
    assert "src/api/x.py" in r.dirty and ".ateam-scratch/handoff.yaml" not in r.dirty
    assert not r.ok

    g.run("add", "src/api/x.py"); g.run("commit", "-q", "-m", "c")
    (target_repo / "notes/out.txt").unlink(); (target_repo / "notes").rmdir()
    st2 = tree_state(g, base)
    r2 = scope_check(st2, allowed=["src/api/**"], incidental=[])
    assert r2.ok and r2.outside == [] and r2.dirty == [] and st2.changed_vs_base == ["src/api/x.py"]


def test_committed_change_outside_paths_is_outside(target_repo):
    g = GitRepo(target_repo); base = g.head()
    (target_repo / "src/app.py").write_text("changed\n"); (target_repo / "requirements.txt").write_text("click\n")
    g.run("add", "-A"); g.run("commit", "-q", "-m", "c")
    r = scope_check(tree_state(g, base), allowed=["src/api/**"], incidental=["requirements.txt"])
    assert r.outside == ["src/app.py"] and not r.ok
