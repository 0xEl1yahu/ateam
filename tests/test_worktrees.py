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


def test_a_follower_shares_the_lane_worktree_with_the_head_as_base(target_repo):
    from ateam.worktrees import contract_worktree, worktree_path
    paths, db, owner = _setup(target_repo)
    head = _verified_head(db, contract_worktree(paths, db, owner, "P-0001", "C-1"), "C-1", "one.txt")
    w = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    assert w.path == home_dir(target_repo) / "worktrees" / "C-1" and w.branch == "ateam/C-1" and w.base == head
    assert db.get_meta("lane:C-2") == "C-1" and db.get_meta("lane:C-1") == "C-1" and db.get_meta("base:C-2") == head
    assert worktree_path(paths, db, "C-2") == w.path and not (home_dir(target_repo) / "worktrees" / "C-2").exists()


def test_a_follower_reuses_its_recorded_lane_and_base(target_repo):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    head = _verified_head(db, w1, "C-1", "one.txt")
    contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    _verified_head(db, w1, "C-1", "newer.txt")
    assert contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1").base == head


def test_a_third_contract_follows_the_lane_head_not_its_predecessor(target_repo):
    from ateam.worktrees import contract_worktree
    paths, db, owner = _setup(target_repo)
    w1 = contract_worktree(paths, db, owner, "P-0001", "C-1")
    _verified_head(db, w1, "C-1", "one.txt")
    w2 = contract_worktree(paths, db, owner, "P-0001", "C-2", after="C-1")
    head2 = _verified_head(db, w2, "C-2", "two.txt")
    w3 = contract_worktree(paths, db, owner, "P-0001", "C-3", after="C-2")
    assert w3.path == w1.path and w3.base == head2 and db.get_meta("lane:C-3") == "C-1"


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
