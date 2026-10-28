"""Contract worktrees under ATeam's home, cut from the integration-branch tip with the base recorded first."""

import subprocess

import pytest

from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir
from ateam.state.db import StateDB


def _setup(target_repo):
    owner = GitRepo(target_repo)
    owner.create_branch("ateam/P-0001", owner.head())
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    return TargetPaths.from_root(target_repo), db, owner


def _advance_integration(owner, tmp_path, rel, name):
    wt = tmp_path / name
    owner.worktree_add_existing(wt, "ateam/P-0001")
    (wt / rel).parent.mkdir(parents=True, exist_ok=True); (wt / rel).write_text("dep\n")
    g = GitRepo(wt); g.run("add", "-A"); g.run("commit", "-q", "-m", f"merge {rel}")
    head = g.head()
    owner.run("worktree", "remove", str(wt))
    return head


def test_cut_from_integration_tip_with_base_recorded(target_repo):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    w = contract_worktree(paths, db, owner, "P-0001", "C-0002")
    assert w.path == home_dir(target_repo) / "worktrees" / "C-0002" and w.branch == "ateam/C-0002"
    assert w.base == owner.rev_parse("ateam/P-0001") == db.get_meta("base:C-0002")
    wt = GitRepo(w.path)
    assert wt.current_branch() == "ateam/C-0002" and wt.head() == w.base and wt.is_linked_worktree()
    assert target_repo.resolve() not in w.path.resolve().parents


def test_rerun_reuses_recorded_base_not_new_tip(target_repo, tmp_path):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    first = contract_worktree(paths, db, owner, "P-0001", "C-0002")
    _advance_integration(owner, tmp_path, "docs/later.md", "adv")
    again = contract_worktree(paths, db, owner, "P-0001", "C-0002")
    assert again == first and again.base != owner.rev_parse("ateam/P-0001")


def test_missing_worktree_is_recreated_at_the_recorded_base(target_repo, tmp_path):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    db.set_meta("base:C-0002", owner.rev_parse("ateam/P-0001"))      # a crash after the meta write
    _advance_integration(owner, tmp_path, "docs/later.md", "adv")
    w = contract_worktree(paths, db, owner, "P-0001", "C-0002")
    assert GitRepo(w.path).head() == db.get_meta("base:C-0002") != owner.rev_parse("ateam/P-0001")


def test_dependent_cut_after_merge_contains_dependency(target_repo, tmp_path):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    early = contract_worktree(paths, db, owner, "P-0001", "C-0002")
    _advance_integration(owner, tmp_path, "src/a/a.py", "dep")
    late = contract_worktree(paths, db, owner, "P-0001", "C-0003")
    assert (late.path / "src" / "a" / "a.py").is_file() and not (early.path / "src" / "a" / "a.py").exists()


def test_wrong_branch_or_foreign_history_refused(target_repo, tmp_path):
    from ateam.worktrees import WorktreeError, contract_worktree
    paths, db, owner = _setup(target_repo)
    w = contract_worktree(paths, db, owner, "P-0001", "C-0002")
    GitRepo(w.path).run("checkout", "-q", "-b", "elsewhere")
    with pytest.raises(WorktreeError, match="on elsewhere, not ateam/C-0002"):
        contract_worktree(paths, db, owner, "P-0001", "C-0002")
    stray = home_dir(target_repo) / "worktrees" / "C-0004"; stray.mkdir(parents=True)
    with pytest.raises(WorktreeError, match="not a worktree"):
        contract_worktree(paths, db, owner, "P-0001", "C-0004")


# ---- lanes (2026-10-27-lanes-design.md, decision 2)

def _verified_head(db, w, cid, name):
    g = GitRepo(w.path)
    (w.path / name).write_text(f"{name}\n"); g.run("add", "-A"); g.run("commit", "-q", "-m", name)
    db.set_checkpoint(cid, None, {"head": g.head()})
    return g.head()


# Owner decision 7 of the 2026-10-28 design: a later contract's base is the integration tip, not its predecessor's head.
def test_a_follower_fast_forwards_to_the_integration_tip_as_its_base(target_repo, tmp_path):
    from ateam.worktrees import contract_worktree, worktree_path
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    _verified_head(db, w1, "C-1", "one.txt")
    owner.run("branch", "-f", "ateam/P-0001", "ateam/C-1")   # the predecessor is integrated
    tip = _advance_integration(owner, tmp_path, "docs/other.md", "other")
    w = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    assert w.path == home_dir(target_repo) / "worktrees" / "C-1" and w.branch == "ateam/C-1" and w.base == tip
    assert db.get_meta("lane:C-2") == "C-1" and db.get_meta("lane:C-1") == "C-1" and db.get_meta("base:C-2") == tip
    assert GitRepo(w.path).head() == tip and (w.path / "docs" / "other.md").is_file()
    assert worktree_path(paths, db, "C-2") == w.path and not (home_dir(target_repo) / "worktrees" / "C-2").exists()


def test_a_follower_holds_a_dependency_integrated_from_another_lane(target_repo, tmp_path):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    _verified_head(db, w1, "C-1", "one.txt")
    owner.run("branch", "-f", "ateam/P-0001", "ateam/C-1")
    _advance_integration(owner, tmp_path, "src/dep/dep.py", "dep")
    w = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    assert (w.path / "src" / "dep" / "dep.py").is_file() and (w.path / "one.txt").is_file()


def test_a_follower_base_is_recorded_once_and_the_fast_forward_completes(target_repo, tmp_path):
    from ateam.worktrees import WorktreeError, contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    owner.run("branch", "-f", "ateam/P-0001", "ateam/C-1")
    tip = _advance_integration(owner, tmp_path, "docs/a.md", "a")
    db.set_meta("base:C-2", tip); db.set_meta("lane:C-2", "C-1")   # a crash after recording, before the fast-forward
    newer = _advance_integration(owner, tmp_path, "docs/b.md", "b")
    w = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    assert w.base == tip != newer and GitRepo(w.path).head() == tip
    assert contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1").base == tip
    # without `after` nothing fast-forwards: a HEAD behind the base is refused as before
    db.set_meta("base:C-1", newer)
    with pytest.raises(WorktreeError, match="does not descend"):
        contract_worktree(paths, db, owner, "P-0001", "C-1")


def test_a_follower_with_leftovers_refuses_before_the_fast_forward(target_repo, tmp_path):
    from ateam.worktrees import WorktreeError, contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    _advance_integration(owner, tmp_path, "docs/a.md", "a")
    (w1.path / "leftover.txt").write_text("x\n")
    with pytest.raises(WorktreeError, match=r"the lane worktree has files left by an earlier contract \(leftover.txt\)"):
        contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    assert GitRepo(w1.path).head() == w1.base


def test_a_follower_with_a_missing_worktree_is_recreated_or_refused(target_repo, tmp_path):
    import shutil
    from ateam.worktrees import WorktreeError, contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    tip = _advance_integration(owner, tmp_path, "docs/a.md", "a")
    owner.run("worktree", "remove", "--force", str(w1.path))
    w = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    assert w.path == w1.path and GitRepo(w.path).head() == tip == w.base
    # a branch that neither descends from nor is an ancestor of the base is refused
    owner.run("worktree", "remove", "--force", str(w.path))
    owner.run("branch", "-f", "ateam/C-1", owner.run("commit-tree", "-m", "orphan", "HEAD^{tree}").strip())
    with pytest.raises(WorktreeError, match="does not descend"):
        contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")


def test_a_follower_reuses_its_recorded_lane_and_base(target_repo):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    _verified_head(db, w1, "C-1", "one.txt")
    base = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1").base
    _verified_head(db, w1, "C-1", "newer.txt")
    assert contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1").base == base == owner.rev_parse("ateam/P-0001")


# Owner decision 7 of the 2026-10-28 design: a third contract is based on the integration tip, not the lane head.
def test_a_third_contract_follows_the_integration_tip_not_its_predecessor(target_repo, tmp_path):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    _verified_head(db, w1, "C-1", "one.txt")
    owner.run("branch", "-f", "ateam/P-0001", "ateam/C-1")
    contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    head2 = _verified_head(db, w1, "C-2", "two.txt")
    owner.run("branch", "-f", "ateam/P-0001", "ateam/C-1")
    tip = _advance_integration(owner, tmp_path, "docs/x.md", "x")
    w3 = contract_worktree(paths, db, owner, "P-0001", "C-3", after="C-2")
    assert w3.path == w1.path and w3.base == tip != head2 and db.get_meta("lane:C-3") == "C-1"
    assert GitRepo(w3.path).is_ancestor(head2, tip)


def test_a_contract_cut_before_lanes_keeps_its_own_worktree(target_repo):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    _verified_head(db, contract_worktree(paths, db, owner, "P-0001", "C-1"), "C-1", "one.txt")
    old = contract_worktree(paths, db, owner, "P-0001", "C-2")
    db.delete_meta("lane:C-2")   # as cut before lanes existed: base recorded, no lane
    w = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    assert w.path == old.path == home_dir(target_repo) / "worktrees" / "C-2" and w.base == old.base
    assert db.get_meta("lane:C-2") == "C-2"


def test_a_merge_in_progress_onto_the_recorded_base_is_accepted(target_repo, tmp_path):
    from ateam.worktrees import WorktreeError, contract_worktree
    paths, db, owner = _setup(target_repo)
    w = contract_worktree(paths, db, owner, "P-0001", "C-1")
    g = GitRepo(w.path)
    (w.path / "f.txt").write_text("mine\n"); g.run("add", "-A"); g.run("commit", "-q", "-m", "mine")
    tip = _advance_integration(owner, tmp_path, "f.txt", "other")
    subprocess.run(["git", "merge", "--no-ff", "--no-commit", tip], cwd=w.path, capture_output=True)
    assert g.merge_in_progress()
    db.set_meta("base:C-1", tip)
    assert contract_worktree(paths, db, owner, "P-0001", "C-1").base == tip
    db.set_meta("base:C-1", _advance_integration(owner, tmp_path, "g.txt", "unrelated"))
    with pytest.raises(WorktreeError):
        contract_worktree(paths, db, owner, "P-0001", "C-1")


# ---- the rebase BUILD (2026-10-27-lanes-design.md, rulings 6-9)

def _conflicting_tip(owner, tmp_path, w, name="tip"):
    """C-1's worktree and ateam/P-0001 both add f.txt; returns the new integration tip."""
    g = GitRepo(w.path)
    (w.path / "f.txt").write_text("mine\n"); g.run("add", "-A"); g.run("commit", "-q", "-m", "mine")
    return _advance_integration(owner, tmp_path, "f.txt", name)


def test_prepare_rebase_records_then_merges_and_is_idempotent(target_repo, tmp_path):
    import json
    from ateam.worktrees import contract_worktree, prepare_rebase
    paths, db, owner = _setup(target_repo)
    w = contract_worktree(paths, db, owner, "P-0001", "C-1")
    tip = _conflicting_tip(owner, tmp_path, w)
    a = db.start_attempt("C-1", "build", "light", "m", {}); db.finish_attempt(a, "completed", None, 1, 1, 0.1, 1, 0.1, "s")
    wt, conflicts = prepare_rebase(paths, db, owner, "P-0001", "C-1", tip)
    assert conflicts == ["f.txt"] and wt.path == w.path and wt.base == tip == db.get_meta("base:C-1")
    assert json.loads(db.get_meta("rebase:C-1")) == {"window": db.window_start("C-1"), "tip": tip, "after": a}
    newer = _advance_integration(owner, tmp_path, "g.txt", "newer")
    assert prepare_rebase(paths, db, owner, "P-0001", "C-1", newer)[1] == ["f.txt"]   # the open merge is kept
    assert db.get_meta("base:C-1") == tip and GitRepo(w.path).merge_head() == tip
    (w.path / "f.txt").write_text("both\n"); GitRepo(w.path).run("commit", "-q", "-am", "resolved")
    assert prepare_rebase(paths, db, owner, "P-0001", "C-1", tip)[1] == [] and db.get_meta("base:C-1") == tip


def test_prepare_rebase_leaves_the_base_when_the_merge_fails(target_repo, tmp_path):
    from ateam.gitops import GitError
    from ateam.worktrees import contract_worktree, prepare_rebase
    paths, db, owner = _setup(target_repo)
    w = contract_worktree(paths, db, owner, "P-0001", "C-1")
    old = db.get_meta("base:C-1")
    tip = _advance_integration(owner, tmp_path, "new.txt", "tip")
    (w.path / "new.txt").write_text("untracked\n")
    with pytest.raises(GitError):
        prepare_rebase(paths, db, owner, "P-0001", "C-1", tip)
    assert db.get_meta("base:C-1") == old and not GitRepo(w.path).merge_in_progress()


def test_rebased_this_window_needs_a_build_after_the_key(target_repo, tmp_path):
    from ateam.worktrees import contract_worktree, prepare_rebase, rebased_this_window
    paths, db, owner = _setup(target_repo)
    w = contract_worktree(paths, db, owner, "P-0001", "C-1")
    assert not rebased_this_window(db, "C-1")
    prepare_rebase(paths, db, owner, "P-0001", "C-1", _conflicting_tip(owner, tmp_path, w))
    assert not rebased_this_window(db, "C-1")          # prepared, but its BUILD has not run (a crash here re-runs it)
    a = db.start_attempt("C-1", "build", "light", "m", {}); db.finish_attempt(a, "interrupted", None, 1, 1, 0.1, 1, 0.1, "s")
    assert not rebased_this_window(db, "C-1")          # an interrupted BUILD does not count
    b = db.start_attempt("C-1", "build", "light", "m", {}); db.finish_attempt(b, "completed", None, 1, 1, 0.1, 1, 0.1, "s")
    assert rebased_this_window(db, "C-1")
    db.resume_pause(None, ["C-1"])
    assert not rebased_this_window(db, "C-1")          # a resume opens a new window


def test_prepare_rebase_after_a_concluded_rebase_merges_a_newer_tip(target_repo, tmp_path):
    """Resume after a second conflict: the earlier rebase's merge was concluded, and the newer tip must be merged."""
    import json
    from ateam.worktrees import contract_worktree, prepare_rebase
    paths, db, owner = _setup(target_repo)
    w = contract_worktree(paths, db, owner, "P-0001", "C-1")
    first = _conflicting_tip(owner, tmp_path, w)
    prepare_rebase(paths, db, owner, "P-0001", "C-1", first)
    (w.path / "f.txt").write_text("both\n"); GitRepo(w.path).run("commit", "-q", "-am", "resolved")
    side = tmp_path / "newer"; owner.worktree_add_existing(side, "ateam/P-0001")
    (side / "f.txt").write_text("newer\n"); g = GitRepo(side); g.run("commit", "-q", "-am", "newer"); newer = g.head()
    owner.run("worktree", "remove", str(side))   # f.txt again: conflicts with the resolution
    _, conflicts = prepare_rebase(paths, db, owner, "P-0001", "C-1", newer)
    assert conflicts == ["f.txt"] and GitRepo(w.path).merge_head() == newer
    assert json.loads(db.get_meta("rebase:C-1"))["tip"] == newer == db.get_meta("base:C-1")
