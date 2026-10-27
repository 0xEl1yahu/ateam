"""ateam integrate, end to end on a scratch target: two contracts on disjoint files, verified for real."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ateam.cli import Context, main
from ateam.config import NamedCheck, load_config, save_config
from ateam.contracts.store import load_contracts
from ateam.gitops import GitRepo
from ateam.intent import commit_intent
from ateam.paths import TargetPaths
from ateam.plans.store import load_plans
from ateam.proposal import parse_header, proposal_relpath


def run(*args):
    return CliRunner().invoke(main, list(args))


def _report():
    def contract(name, depends_on):
        return {"title": f"Add {name}", "paths": [f"src/{name}/**", f"tests/test_{name}.py"], "incidental": [], "depends_on": depends_on,
                "acceptance": [{"id": "A1", "statement": f"{name}() works", "check": {"type": "test", "ref": f"tests/test_{name}.py::test_{name}"}},
                               {"id": "A2", "statement": f"{name} reads well", "check": {"type": "judgment"}}],
                "checks": ["unit"], "allocation": {"complexity": "low", "risk": "low", "uncertainty": "low", "reasoning": "small",
                                                   "tier": {"build": "light", "test": "light", "investigate": "none"}}}
    return {"title": "Two parts", "contracts": [contract("a", []), contract("b", [0])], "proposed_records": [],
            "unknowns": ["one open question"], "notes": "n"}


def _fake_planner(monkeypatch, report):
    from ateam.runner import investigate as inv_mod
    from ateam.runner.run import WorkerResult

    async def fake(**kw):
        db = kw["db"]
        a = db.start_attempt(kw["name"], "investigate", "light", "haiku", {})
        db.finish_attempt(a, "completed", None, 1, 1, 0.001, 1, 0.1, "s")
        res = WorkerResult("success", False, "completed", "s", 1, 0.001, {}, {}, report, [], [], None, 0.1)
        return inv_mod.InvestigationResult(res=res, attempt_id=a, tampered=[])

    monkeypatch.setattr("ateam.cli.run_investigation", fake, raising=False)
    monkeypatch.setattr(inv_mod, "run_investigation", fake)


def _work(root, tmp_path, cid, name, base="main"):
    wt = tmp_path / f"wt-{name}"
    GitRepo(root).worktree_add(wt, f"ateam/{cid}", base)
    (wt / "src" / name).mkdir(parents=True)
    (wt / "src" / name / f"{name}.py").write_text(f"def {name}():\n    return '{name}'\n")
    (wt / "tests" / f"test_{name}.py").write_text(f"def test_{name}():\n    assert True\n")
    g = GitRepo(wt); g.run("add", "-A"); g.run("commit", "-q", "-m", f"add {name}")
    return wt


def _commit_on_main(root, rel, text):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)
    g = GitRepo(root); g.run("add", "-A"); g.run("commit", "-q", "-m", f"main: {rel}")
    return g.head()


def _set_config(paths, **updates):
    save_config(paths, load_config(paths).model_copy(update=updates))
    commit_intent(paths, "config", ["config.yaml"])


@pytest.fixture
def approved(target_repo, monkeypatch):
    """P-0001 approved: C-0001 (src/a) ready, C-0002 (src/b, depends on C-0001) draft. Config check `unit: true`."""
    paths = TargetPaths.from_root(target_repo)
    _set_config(paths, checks=[NamedCheck(name="unit", cmd="true")])
    _fake_planner(monkeypatch, _report())
    assert run("plan", "two parts", "--no-review", "--target", str(target_repo)).exit_code == 0
    assert run("approve", "P-0001", "--skip-records", "--target", str(target_repo)).exit_code == 0
    return SimpleNamespace(root=target_repo, paths=paths)


@pytest.fixture
def verified(approved, tmp_path):
    """Both contracts verified through `ateam verify` in their own worktrees."""
    root = approved.root
    wa = _work(root, tmp_path, "C-0001", "a")
    assert run("verify", "C-0001", "--cwd", str(wa), "--target", str(root)).exit_code == 0
    assert run("contracts", "refresh", "--target", str(root)).exit_code == 0
    wb = _work(root, tmp_path, "C-0002", "b")
    r = run("verify", "C-0002", "--cwd", str(wb), "--target", str(root))
    assert r.exit_code == 0, r.output
    return approved


def _integrate(root):
    from ateam.integrate import integrate
    out: list[str] = []
    ctx = Context(root)
    return integrate(ctx, "P-0001", echo=out.append), out, ctx


def test_happy_path_reaches_merge_proposed(verified):
    from ateam.integrate import integration_branch
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "merge_proposed", out
    owner = GitRepo(verified.root)
    tip = owner.rev_parse(integration_branch("P-0001"))
    log = owner.run("log", "--format=%s", tip)
    assert "ateam: integrate C-0001 into ateam/P-0001" in log and "ateam: integrate C-0002 into ateam/P-0001" in log
    cs = load_contracts(verified.paths.contracts)
    assert cs["C-0001"].status == "integrated" == cs["C-0002"].status
    assert ctx.db.contract_status("C-0001") == "integrated" == ctx.db.contract_status("C-0002")
    intent = GitRepo(verified.paths.intent)
    header = parse_header(intent.show_file("HEAD", proposal_relpath("P-0001")))
    assert header.tip == tip == res.tip and header.target_tip == owner.rev_parse("main") and header.branch == "ateam/P-0001"
    text = (verified.paths.intent / proposal_relpath("P-0001")).read_text()
    assert "one open question" in text and "C-0001 A2 (judgment) not checked" in text and "src/a/a.py" in text
    assert load_plans(verified.paths.plans)["P-0001"].status == "merge_proposed" == ctx.db.feature_status("P-0001")
    assert intent.is_clean() and owner.current_branch() == "main"


def test_refuses_unverified_contract(approved):
    from ateam.integrate import IntegrateRefused
    with pytest.raises(IntegrateRefused, match="C-0001 is ready in its file and ready in the state store"):
        _integrate(approved.root)
    assert Context(approved.root).db.feature_status("P-0001") == "plan_approved"


def test_conflict_pauses_with_environment_failure(verified):
    _commit_on_main(verified.root, "src/a/a.py", "A = 'main'\n")      # main now adds the same file C-0001 adds
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "conflict"
    assert ctx.db.feature_status("P-0001") == "paused" and load_plans(verified.paths.plans)["P-0001"].status == "paused"
    reason = ctx.db.transitions("feature", "P-0001")[-1]["reason"]
    assert reason == "environment_failure: C-0001 conflicts in src/a/a.py"
    assert ctx.db.contract_status("C-0001") == "verified"
    from ateam.integrate import IntegrateRefused
    with pytest.raises(IntegrateRefused, match="paused.*environment_failure"):
        _integrate(verified.root)


def test_failing_final_check_pauses(verified):
    _set_config(verified.paths, checks=[NamedCheck(name="unit", cmd="true"), NamedCheck(name="lint", cmd="echo lint broke; false")])
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "checks_failed"
    assert ctx.db.transitions("feature", "P-0001")[-1]["reason"] == "verification_unresolvable: lint failed"
    assert any("lint broke" in line for line in out)
    assert ctx.db.contract_status("C-0001") == "integrated"   # the merges stand; the plan is paused for the owner


def test_rerun_after_moved_target_merges_it_in_and_refreshes_the_proposal(verified):
    first, _, _ = _integrate(verified.root)
    again, out, _ = _integrate(verified.root)
    assert again.outcome == "merge_proposed" and again.tip == first.tip and "already proposed" in " ".join(again.messages)
    moved = _commit_on_main(verified.root, "docs/notes.md", "moved\n")
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "merge_proposed" and res.tip != first.tip
    owner = GitRepo(verified.root)
    assert owner.is_ancestor(moved, res.tip) and "ateam: merge main into ateam/P-0001" in owner.run("log", "-1", "--format=%s", res.tip)
    header = parse_header(GitRepo(verified.paths.intent).show_file("HEAD", proposal_relpath("P-0001")))
    assert header.tip == res.tip and header.target_tip == moved
    assert [t["to_status"] for t in ctx.db.transitions("feature", "P-0001")][-2:] == ["integrating", "merge_proposed"]


def test_reconcile_replays_a_contract_step_lost_in_a_crash(verified, monkeypatch):
    from ateam.state.db import StateDB
    real = StateDB.transition

    def crash_on_c2(self, kind, id, to, by, reason=None):
        if kind == "contract" and id == "C-0002" and to == "integrated":
            raise KeyboardInterrupt
        return real(self, kind, id, to, by, reason)

    with monkeypatch.context() as m:
        m.setattr(StateDB, "transition", crash_on_c2)
        with pytest.raises(KeyboardInterrupt):
            _integrate(verified.root)
    assert load_contracts(verified.paths.contracts)["C-0002"].status == "integrated"
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "merge_proposed" and "reconciled: C-0002 -> integrated (state store)" in out
    assert ctx.db.contract_status("C-0002") == "integrated"


def test_reconcile_replays_a_feature_step_lost_in_a_crash(verified, monkeypatch):
    from ateam.state.db import StateDB
    real = StateDB.transition

    def crash_on_proposed(self, kind, id, to, by, reason=None):
        if kind == "feature" and to == "merge_proposed":
            raise KeyboardInterrupt
        return real(self, kind, id, to, by, reason)

    with monkeypatch.context() as m:
        m.setattr(StateDB, "transition", crash_on_proposed)
        with pytest.raises(KeyboardInterrupt):
            _integrate(verified.root)
    assert load_plans(verified.paths.plans)["P-0001"].status == "merge_proposed"
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "merge_proposed" and "reconciled: P-0001 -> merge_proposed (state store)" in out
    assert ctx.db.feature_status("P-0001") == "merge_proposed"


def _prepare_by_hand(verified):
    """The integration start, branch and worktree as integrate would leave them, without any merge."""
    from ateam.integrate import integration_branch, integration_worktree
    owner = GitRepo(verified.root)
    ctx = Context(verified.root)
    ctx.db.set_meta("integration:P-0001", json.dumps({"branch": "ateam/P-0001", "start": owner.head(), "target_branch": "main"}))
    owner.create_branch(integration_branch("P-0001"), owner.head())
    wt = integration_worktree(verified.paths, "P-0001")
    wt.parent.mkdir(parents=True, exist_ok=True)
    owner.worktree_add_existing(wt, "ateam/P-0001")
    return GitRepo(wt)


def test_reused_worktree_with_a_merge_in_progress_is_refused(verified):
    from ateam.integrate import IntegrateRefused
    wt = _prepare_by_hand(verified)
    head = Context(verified.root).db.latest_checkpoint("C-0001")["head"]
    wt.run("merge", "--no-ff", "--no-commit", head)
    with pytest.raises(IntegrateRefused, match="a merge is in progress"):
        _integrate(verified.root)


def test_reused_worktree_with_untracked_files_is_refused(verified):
    from ateam.integrate import IntegrateRefused
    wt = _prepare_by_hand(verified)
    (wt.root / "stray.txt").write_text("x\n")
    with pytest.raises(IntegrateRefused, match="uncommitted changes.*stray.txt"):
        _integrate(verified.root)


def test_recorded_start_without_branch_recreates_the_branch(verified):
    owner = GitRepo(verified.root)
    start = owner.head()
    Context(verified.root).db.set_meta("integration:P-0001", json.dumps({"branch": "ateam/P-0001", "start": start, "target_branch": "main"}))
    res, out, _ = _integrate(verified.root)
    assert res.outcome == "merge_proposed" and owner.is_ancestor(start, res.tip)
    assert any("created ateam/P-0001 at" in line for line in out)


def test_branch_not_descending_from_the_recorded_start_is_refused(verified):
    from ateam.integrate import IntegrateRefused
    owner = GitRepo(verified.root)
    old = owner.head()
    new = _commit_on_main(verified.root, "docs/x.md", "x\n")
    Context(verified.root).db.set_meta("integration:P-0001", json.dumps({"branch": "ateam/P-0001", "start": new, "target_branch": "main"}))
    owner.create_branch("ateam/P-0001", old)
    with pytest.raises(IntegrateRefused, match="does not descend from the recorded start"):
        _integrate(verified.root)


def test_setup_failure_pauses(verified):
    _set_config(verified.paths, setup=["echo no venv; false"])
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "setup_failed"
    assert ctx.db.transitions("feature", "P-0001")[-1]["reason"].startswith("environment_failure: setup failed")
    assert ctx.db.feature_status("P-0001") == "paused"


# ---- final review fixes

def _interrupt_final_checks(monkeypatch, root):
    import ateam.verify as v

    def boom(*a, **kw):
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(v, "run_checks", boom)
        with pytest.raises(KeyboardInterrupt):
            _integrate(root)


def test_recreated_branch_without_integrated_work_is_refused(verified, monkeypatch):
    from ateam.integrate import IntegrateRefused, integration_worktree
    _interrupt_final_checks(monkeypatch, verified.root)
    owner = GitRepo(verified.root)
    owner.run("worktree", "remove", "--force", str(integration_worktree(verified.paths, "P-0001")))
    owner.run("branch", "-D", "ateam/P-0001")
    with pytest.raises(IntegrateRefused, match="C-0001 is integrated but its verified head .* is not on ateam/P-0001"):
        _integrate(verified.root)


def test_reset_integration_worktree_is_refused(verified, monkeypatch):
    from ateam.integrate import IntegrateRefused, integration_worktree
    _interrupt_final_checks(monkeypatch, verified.root)
    wt = GitRepo(integration_worktree(verified.paths, "P-0001"))
    wt.run("reset", "-q", "--hard", GitRepo(verified.root).rev_parse("main"))
    with pytest.raises(IntegrateRefused, match="C-0001 is integrated but its verified head"):
        _integrate(verified.root)


def test_unproposed_commits_on_the_branch_are_reproposed(verified):
    from ateam.integrate import integration_worktree
    first, _, _ = _integrate(verified.root)
    wt = GitRepo(integration_worktree(verified.paths, "P-0001"))
    (wt.root / "fix.txt").write_text("owner fix\n"); wt.run("add", "-A"); wt.run("commit", "-q", "-m", "owner fix")
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "merge_proposed" and res.tip == wt.head() != first.tip
    assert parse_header(GitRepo(verified.paths.intent).show_file("HEAD", proposal_relpath("P-0001"))).tip == wt.head()
    assert "ateam/P-0001 has commits the proposal does not cover" in ctx.db.transitions("feature", "P-0001")[-2]["reason"]


def test_parallel_intent_commit_during_checks_is_a_retry_not_a_pause(verified, monkeypatch):
    import ateam.verify as v
    from ateam.integrate import IntegrateRefused
    real = v.run_checks

    def with_parallel_commit(*a, **kw):
        f = verified.paths.intent / "notes.md"; f.write_text("someone else\n")
        commit_intent(verified.paths, "parallel", ["notes.md"])
        return real(*a, **kw)

    with monkeypatch.context() as m:
        m.setattr(v, "run_checks", with_parallel_commit)
        with pytest.raises(IntegrateRefused) as e:
            _integrate(verified.root)
    message = str(e.value).lower()
    assert "committed during the final checks" in message and "re-run ateam integrate p-0001" in message
    ctx = Context(verified.root)
    assert ctx.db.feature_status("P-0001") == "integrating" and load_plans(verified.paths.plans)["P-0001"].status == "integrating"
    res, out, _ = _integrate(verified.root)
    assert res.outcome == "merge_proposed"


def test_check_that_edits_the_worktree_still_pauses_as_tamper(verified):
    _set_config(verified.paths, checks=[NamedCheck(name="unit", cmd="true"), NamedCheck(name="sneaky", cmd="echo x >> src/a/a.py")])
    res, out, ctx = _integrate(verified.root)
    assert res.outcome == "checks_failed"
    assert ctx.db.transitions("feature", "P-0001")[-1]["reason"] == "verification_unresolvable: tamper: worktree tracked files changed"


def test_proposal_shows_record_status_and_final_check_results(verified):
    res, _, _ = _integrate(verified.root)
    text = res.proposal.read_text()
    assert "## Final checks on the integration tip" in text and "- unit: passed" in text


# ---- public helpers for the scheduler (plan 2026-10-26-scheduler, task 4)

def test_prepare_integration_is_idempotent(verified):
    from ateam.integrate import integration_branch, prepare_integration
    ctx = Context(verified.root)
    tip = GitRepo(verified.root).rev_parse("main")
    out: list[str] = []
    wt = prepare_integration(ctx, "P-0001", tip, out.append)
    start = ctx.db.get_meta("integration:P-0001")
    again = prepare_integration(ctx, "P-0001", _commit_on_main(verified.root, "docs/later.md", "x\n"), out.append)
    assert again.root == wt.root and ctx.db.get_meta("integration:P-0001") == start
    assert GitRepo(verified.root).rev_parse(integration_branch("P-0001")) == tip   # the recorded start, not the new tip


def test_merge_contract_records_and_tolerates_a_repeat(verified):
    from ateam.integrate import merge_contract, prepare_integration
    ctx = Context(verified.root)
    wt = prepare_integration(ctx, "P-0001", GitRepo(verified.root).rev_parse("main"), lambda m: None)
    sha = merge_contract(ctx, wt, "P-0001", "C-0001", lambda m: None)
    assert wt.head() == sha and wt.is_ancestor(ctx.db.latest_checkpoint("C-0001")["head"], sha)
    assert load_contracts(verified.paths.contracts)["C-0001"].status == "integrated" == ctx.db.contract_status("C-0001")
    intent_head = GitRepo(verified.paths.intent).head()
    assert merge_contract(ctx, wt, "P-0001", "C-0001", lambda m: None) == sha
    assert GitRepo(verified.paths.intent).head() == intent_head and wt.head() == sha


def test_merge_contract_conflict_records_nothing(verified):
    from ateam.gitops import MergeConflict
    from ateam.integrate import merge_contract, prepare_integration
    _commit_on_main(verified.root, "src/a/a.py", "A = 'main'\n")
    ctx = Context(verified.root)
    wt = prepare_integration(ctx, "P-0001", GitRepo(verified.root).rev_parse("main"), lambda m: None)
    intent_head = GitRepo(verified.paths.intent).head()
    with pytest.raises(MergeConflict):
        merge_contract(ctx, wt, "P-0001", "C-0001", lambda m: None)
    assert load_contracts(verified.paths.contracts)["C-0001"].status == "verified" == ctx.db.contract_status("C-0001")
    assert GitRepo(verified.paths.intent).head() == intent_head and wt.status_porcelain() == []


def test_integration_conflict_records_a_conflict_pause(verified):
    _commit_on_main(verified.root, "src/a/a.py", "A = 'main'\n")
    res, out, ctx = _integrate(verified.root)
    p = ctx.db.open_pause("P-0001")
    assert res.outcome == "conflict" and p["conflict"] is True and p["reason"] == "environment_failure"
    assert [c["contract_id"] for c in p["contracts"]] == ["C-0001"] and p["contracts"][0]["evidence"] == {"paths": ["src/a/a.py"]}


def test_failed_final_checks_record_a_pause_that_is_not_a_conflict(verified):
    _set_config(verified.paths, checks=[NamedCheck(name="unit", cmd="true"), NamedCheck(name="lint", cmd="false")])
    res, out, ctx = _integrate(verified.root)
    p = ctx.db.open_pause("P-0001")
    assert p["conflict"] is False and p["reason"] == "verification_unresolvable" and p["detail"] == "lint failed" and p["contracts"] == []


# ---- the rebase BUILD's state step (2026-10-27-lanes-design.md, ruling 6)

def test_reopen_for_rebase_moves_file_then_store_and_repeats_safely(verified):
    from ateam.integrate import reopen_for_rebase
    ctx, out = Context(verified.root), []
    tip = GitRepo(verified.root).head()
    reopen_for_rebase(ctx, "P-0001", "C-0001", tip, out.append)
    assert load_contracts(verified.paths.contracts)["C-0001"].status == "building"
    assert ctx.db.contract_status("C-0001") == "building"
    assert GitRepo(verified.paths.intent).is_clean() and "rebase C-0001" in GitRepo(verified.paths.intent).run("log", "-1", "--format=%s")
    reopen_for_rebase(ctx, "P-0001", "C-0001", tip, out.append)
    assert [t["to_status"] for t in ctx.db.transitions("contract", "C-0001")].count("building") == 2   # verify's walk + this one


def _mid_rebase(verified, tmp_path, window_now=True, merging=True):
    """C-0001: file `building`, store `verified`, a rebase key, and a lane worktree with (or without) the merge open."""
    import json
    from ateam.contracts.model import ContractStatus
    from ateam.contracts.store import save_contract
    from ateam.intent import commit_intent_files
    from ateam.paths import home_dir
    ctx = Context(verified.root)
    owner = GitRepo(verified.root)
    head = ctx.db.latest_checkpoint("C-0001")["head"]
    lane = home_dir(verified.root) / "worktrees" / "L-1"
    owner.worktree_add(lane, "lane-L-1", head)
    owner.create_branch("other", head)
    side = tmp_path / "side"; owner.worktree_add_existing(side, "other")
    (side / "src" / "a" / "a.py").write_text("other\n"); g = GitRepo(side); g.run("commit", "-q", "-am", "other"); tip = g.head()
    (lane / "src" / "a" / "a.py").write_text("mine\n"); GitRepo(lane).run("commit", "-q", "-am", "mine")
    if merging:
        GitRepo(lane).merge_keep_conflicts(tip)
    ctx.db.set_meta("lane:C-0001", "L-1")
    ctx.db.set_meta("rebase:C-0001", json.dumps({"window": ctx.db.window_start("C-0001") + (0 if window_now else -1), "tip": tip, "after": 0}))
    c = load_contracts(verified.paths.contracts)["C-0001"]
    saved = save_contract(verified.paths.contracts, c.model_copy(update={"status": ContractStatus.building}))
    commit_intent_files(verified.paths, "ateam: rebase C-0001", [saved])
    return ctx, owner


@pytest.mark.parametrize("window_now, merging, reconciled", [(True, True, True), (False, True, False), (True, False, False)])
def test_reconcile_completes_a_rebase_whose_store_step_was_lost(verified, tmp_path, window_now, merging, reconciled):
    from ateam.integrate import IntegrateRefused, reconcile_contracts
    ctx, owner = _mid_rebase(verified, tmp_path, window_now, merging)
    cs, out = load_contracts(verified.paths.contracts), []
    if reconciled:
        reconcile_contracts(ctx.db, owner, cs, ["C-0001"], "ateam/P-0001", out.append)
        assert ctx.db.contract_status("C-0001") == "building" and "reconciled" in out[0]
    else:
        with pytest.raises(IntegrateRefused):
            reconcile_contracts(ctx.db, owner, cs, ["C-0001"], "ateam/P-0001", out.append)
