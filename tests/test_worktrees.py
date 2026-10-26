"""Contract worktrees under ATeam's home, cut from the integration-branch tip with the base recorded first."""

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
