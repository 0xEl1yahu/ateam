"""ateam merge: fast-forward only, in the owner's checkout, trusting only the committed proposal."""

import pytest

from ateam.cli import Context
from ateam.gitops import GitRepo
from ateam.plans.store import load_plans
from ateam.proposal import proposal_relpath
from test_integrate import _commit_on_main, _integrate, approved, verified  # noqa: F401  (fixtures)


@pytest.fixture
def proposed(verified):
    res, out, _ = _integrate(verified.root)
    assert res.outcome == "merge_proposed", out
    verified.tip = res.tip
    return verified


def _merge(root):
    from ateam.merge import merge_plan
    ctx = Context(root)
    return merge_plan(ctx, "P-0001"), ctx


def _unchanged(p, ctx, main_before):
    assert GitRepo(p.root).rev_parse("main") == main_before
    assert ctx.db.feature_status("P-0001") == "merge_proposed" == load_plans(p.paths.plans)["P-0001"].status


def test_merge_fast_forwards_and_marks_merged(proposed):
    owner = GitRepo(proposed.root)
    old = owner.head()
    res, ctx = _merge(proposed.root)
    assert res.old_head == old and res.new_head == proposed.tip == owner.head() and res.target_branch == "main"
    assert owner.status_porcelain() == [] and (proposed.root / "src" / "b" / "b.py").is_file()
    assert ctx.db.feature_status("P-0001") == "merged" == load_plans(proposed.paths.plans)["P-0001"].status
    assert GitRepo(proposed.paths.intent).run("log", "-1", "--format=%s").strip() == "ateam: P-0001 -> merged"


def test_refuses_wrong_branch(proposed):
    from ateam.merge import MergeRefused
    owner = GitRepo(proposed.root)
    before = owner.rev_parse("main")
    owner.run("checkout", "-q", "-b", "elsewhere")
    with pytest.raises(MergeRefused, match="on elsewhere, not main"):
        _merge(proposed.root)
    _unchanged(proposed, Context(proposed.root), before)


def test_refuses_dirty_checkout_untracked_included(proposed):
    from ateam.merge import MergeRefused
    before = GitRepo(proposed.root).head()
    (proposed.root / "notes.txt").write_text("mine\n")
    with pytest.raises(MergeRefused, match="uncommitted changes.*notes.txt"):
        _merge(proposed.root)
    _unchanged(proposed, Context(proposed.root), before)


def test_refuses_edited_uncommitted_proposal(proposed):
    from ateam.merge import MergeRefused
    before = GitRepo(proposed.root).head()
    f = proposed.paths.intent / proposal_relpath("P-0001")
    f.write_text(f.read_text().replace(proposed.tip, "0" * 40))
    with pytest.raises(MergeRefused, match="intent repo .* uncommitted changes"):
        _merge(proposed.root)
    _unchanged(proposed, Context(proposed.root), before)


def test_refuses_tip_mismatch(proposed):
    from ateam.integrate import integration_worktree
    from ateam.merge import MergeRefused
    before = GitRepo(proposed.root).head()
    wt = GitRepo(integration_worktree(proposed.paths, "P-0001"))
    (wt.root / "late.txt").write_text("late\n"); wt.run("add", "-A"); wt.run("commit", "-q", "-m", "late")
    with pytest.raises(MergeRefused, match=f"ateam/P-0001 is at {wt.head()[:12]} but the proposal names {proposed.tip[:12]}"):
        _merge(proposed.root)
    _unchanged(proposed, Context(proposed.root), before)


def test_fast_forward_impossible_changes_nothing(proposed):
    from ateam.merge import FastForwardImpossible
    moved = _commit_on_main(proposed.root, "docs/x.md", "x\n")
    with pytest.raises(FastForwardImpossible, match=f"main moved to {moved[:12]}.*{proposed.tip[:12]}.*ateam integrate P-0001"):
        _merge(proposed.root)
    ctx = Context(proposed.root)
    _unchanged(proposed, ctx, moved)
    assert GitRepo(proposed.root).run("log", "-1", "--format=%s").strip() == "main: docs/x.md"


def test_rerun_after_interruption_completes_without_ref_change(proposed, monkeypatch):
    from ateam.state.db import StateDB
    real = StateDB.transition

    def crash(self, kind, id, to, by, reason=None):
        if kind == "feature" and to == "merged":
            raise KeyboardInterrupt
        return real(self, kind, id, to, by, reason)

    with monkeypatch.context() as m:
        m.setattr(StateDB, "transition", crash)
        with pytest.raises(KeyboardInterrupt):
            _merge(proposed.root)
    owner = GitRepo(proposed.root)
    assert owner.head() == proposed.tip and load_plans(proposed.paths.plans)["P-0001"].status == "merged"
    reflog = owner.run("reflog", "-n", "1", "main")
    res, ctx = _merge(proposed.root)
    assert res.new_head == res.old_head == proposed.tip and owner.run("reflog", "-n", "1", "main") == reflog
    assert ctx.db.feature_status("P-0001") == "merged"
