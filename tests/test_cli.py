from click.testing import CliRunner

from ateam.cli import main


def test_version_flag():
    r = CliRunner().invoke(main, ["--version"])
    assert r.exit_code == 0
    assert "0.1.0" in r.output


import yaml

from ateam.contracts.store import load_contracts
from ateam.gitops import GitRepo
from ateam.intent import commit_intent
from ateam.paths import TargetPaths, home_dir
from ateam.records.store import load_records, save_record
from ateam.state.db import StateDB


def run(*args, input=None):
    return CliRunner().invoke(main, list(args), input=input)


def test_records_validate_reports_errors(cli_target, sample_record):
    bad = sample_record.model_copy(update={"id": "R-0002"})
    bad.binds.paths = ["./src/**"]
    save_record(TargetPaths.from_root(cli_target).records, bad)
    r = run("records", "validate", "--target", str(cli_target))
    assert r.exit_code == 1 and "ERROR R-0002" in r.output and "invalid glob" in r.output


def test_records_validate_clean_exits_zero(cli_target):
    r = run("records", "validate", "--target", str(cli_target))
    assert r.exit_code == 0, r.output


def test_records_list_and_show(cli_target):
    assert "R-0001" in run("records", "list", "--target", str(cli_target)).output
    assert "R-0001" not in run("records", "list", "--status", "approved", "--target", str(cli_target)).output
    assert "Reading state" in run("records", "show", "R-0001", "--target", str(cli_target)).output


def test_records_approve_commits_and_stores_hash(cli_target):
    r = run("records", "approve", "R-0001", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    assert GitRepo(TargetPaths.from_root(cli_target).intent).run("log", "-1", "--format=%s").strip() == "ateam: approve R-0001"
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert "R-0001" in db.record_hashes()


def test_records_approve_shows_overlaps(cli_target, sample_record):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    second = sample_record.model_copy(update={"id": "R-0002", "statement": "Books API returns JSON only"})
    save_record(TargetPaths.from_root(cli_target).records, second)
    commit_intent(TargetPaths.from_root(cli_target), "seed 2", ["records"])
    r = run("records", "approve", "R-0002", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    assert "Overlapping approved records:" in r.output and "R-0001" in r.output


def test_records_reject_and_guard_message(cli_target):
    r = run("records", "reject", "R-0001", "covered elsewhere", "--target", str(cli_target))
    assert r.exit_code == 0 and load_records(TargetPaths.from_root(cli_target).records)["R-0001"].status == "rejected"
    r2 = run("records", "approve", "R-0001", "--target", str(cli_target))
    assert r2.exit_code == 1 and "expected proposed" in r2.output


def test_contracts_show_and_freeze_selects_records(cli_target):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    assert "Expose reading state" in run("contracts", "show", "C-0001", "--target", str(cli_target)).output
    r = run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    assert r.exit_code == 0 and "R-0001" in r.output, r.output
    c = load_contracts(TargetPaths.from_root(cli_target).contracts)["C-0001"]
    assert c.records[0].id == "R-0001" and c.records[0].hash and c.status == "ready"
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert db.contract_status("C-0001") == "ready"


def test_status_without_ateam_dir(tmp_path, ateam_home):
    r = run("status", "--target", str(tmp_path))
    assert r.exit_code == 2 and "No intent repo" in r.output and "ateam intent init" in r.output


def test_status_lists_contracts(cli_target):
    run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    r = run("status", "--target", str(cli_target))
    assert r.exit_code == 0 and "C-0001" in r.output and "ready" in r.output


def test_doctor_mentions_versions(cli_target):
    r = run("doctor", "--target", str(cli_target))
    assert r.exit_code == 0 and "SDK 0.2." in r.output and "2.1.310" in r.output and "main" in r.output


def test_records_validate_reports_malformed_file_without_traceback(cli_target):
    (TargetPaths.from_root(cli_target).records / "notes.yaml").write_text("just: notes\n")
    r = run("records", "validate", "--target", str(cli_target))
    assert r.exit_code == 1 and "ERROR" in r.output and "notes.yaml" in r.output and "Traceback" not in r.output


def test_worker_run_refuses_target_checkout_and_non_worktree(cli_target, tmp_path):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    r = run("worker", "run", "C-0001", "--role", "build", "--cwd", str(cli_target), "--target", str(cli_target))
    assert r.exit_code == 4 and "isolated worktree" in r.output
    other = tmp_path / "other"; other.mkdir(); GitRepo(other).run("init", "-q")
    r2 = run("worker", "run", "C-0001", "--role", "build", "--cwd", str(other), "--target", str(cli_target))
    assert r2.exit_code == 4 and "isolated worktree" in r2.output


def test_worker_run_refuses_unfrozen_contract(cli_target, tmp_path):
    wt = tmp_path / "wt"; GitRepo(cli_target).worktree_add(wt, "wt-x", "main")
    r = run("worker", "run", "C-0001", "--role", "build", "--cwd", str(wt), "--target", str(cli_target))
    assert r.exit_code == 4 and "not frozen" in r.output


def test_contracts_freeze_refuses_non_draft_without_force(cli_target):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    assert run("contracts", "freeze", "C-0001", "--target", str(cli_target)).exit_code == 0
    r = run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    assert r.exit_code == 1 and "already frozen" in r.output
    assert run("contracts", "freeze", "C-0001", "--force", "--target", str(cli_target)).exit_code == 0


def test_contracts_validate_reports_hand_edit_and_drift(cli_target, sample_record):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    assert run("contracts", "validate", "--target", str(cli_target)).exit_code == 0
    c = load_contracts(TargetPaths.from_root(cli_target).contracts)["C-0001"]
    from ateam.contracts.store import save_contract
    save_contract(TargetPaths.from_root(cli_target).contracts, c.model_copy(update={"title": "edited by hand"}))
    r = run("contracts", "validate", "--target", str(cli_target))
    assert r.exit_code == 1 and "C-0001" in r.output and "hand edit" in r.output
    save_contract(TargetPaths.from_root(cli_target).contracts, c)
    save_record(TargetPaths.from_root(cli_target).records, load_records(TargetPaths.from_root(cli_target).records)["R-0001"].model_copy(update={"statement": "changed"}))
    r2 = run("contracts", "validate", "--target", str(cli_target))
    assert r2.exit_code == 1 and "drift" in r2.output and "R-0001" in r2.output


def test_schema_command_prints_json_schema():
    import json
    for kind in ("records", "contracts", "config"):
        r = run("schema", kind)
        assert r.exit_code == 0 and json.loads(r.output)["type"] == "object"



def _frozen_target(cli_target):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    run("contracts", "freeze", "C-0001", "--target", str(cli_target))


def test_worker_run_refuses_foreign_and_nested_worktrees(cli_target, tmp_path):
    _frozen_target(cli_target)
    other = tmp_path / "other"; other.mkdir()
    g = GitRepo(other); g.run("init", "-q", "-b", "main"); g.run("config", "user.email", "t@e"); g.run("config", "user.name", "t")
    (other / "f").write_text("x"); g.run("add", "f"); g.run("commit", "-q", "-m", "i")
    foreign_wt = tmp_path / "foreign-wt"; g.worktree_add(foreign_wt, "x", "main")
    r = run("worker", "run", "C-0001", "--role", "build", "--cwd", str(foreign_wt), "--target", str(cli_target))
    assert r.exit_code == 4 and "not a worktree of the target" in r.output
    nested = cli_target / "nested-wt"; GitRepo(cli_target).worktree_add(nested, "nested", "main")
    r2 = run("worker", "run", "C-0001", "--role", "build", "--cwd", str(nested), "--target", str(cli_target))
    assert r2.exit_code == 4 and "inside the target" in r2.output


def test_worker_run_uses_worktree_root_and_prepares_after_guards(cli_target, tmp_path):
    _frozen_target(cli_target)
    wt = tmp_path / "wt"; GitRepo(cli_target).worktree_add(wt, "wt-y", "main")
    r = run("worker", "run", "C-0001", "--role", "investigate", "--cwd", str(wt / "src"), "--target", str(cli_target))
    # investigate has no tier on this contract: clean refusal, exit 1, and no scratch dir created before the guards
    assert r.exit_code == 1 and "no investigate tier" in r.output and "Traceback" not in r.output
    assert not (wt / ".ateam-scratch").exists() and not (wt / "src/.ateam-scratch").exists()


def test_default_base_is_merge_base_with_target_branch(cli_target, tmp_path):
    from ateam.runner.run import default_base
    wt = tmp_path / "wt"; g = GitRepo(cli_target); g.worktree_add(wt, "wt-z", "main")
    fork = g.head()
    (wt / "src/api/x.py").write_text("changed\n"); GitRepo(wt).run("add", "-A"); GitRepo(wt).run("commit", "-q", "-m", "work")
    assert default_base(GitRepo(wt), "main") == fork


def test_malformed_record_file_is_reported_by_every_command(cli_target):
    (TargetPaths.from_root(cli_target).records / "notes.yaml").write_text("just: notes\n")
    for args in (["records", "list"], ["records", "show", "R-0001"], ["records", "approve", "R-0001"], ["contracts", "freeze", "C-0001"], ["brief", "C-0001", "--role", "build"]):
        r = run(*args, "--target", str(cli_target))
        assert r.exit_code == 1 and "notes.yaml" in r.output and "Traceback" not in r.output, args


# ---- Task 6: intent commands, doctor derivation, nothing written into the target

def _fresh_target(tmp_path, ateam_home):
    import subprocess
    t = tmp_path / "fresh"; t.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=t, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=t, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=t, check=True)
    (t / "f").write_text("x"); subprocess.run(["git", "add", "f"], cwd=t, check=True); subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=t, check=True)
    return t


def test_intent_init_then_status_ok(tmp_path, ateam_home):
    t = _fresh_target(tmp_path, ateam_home)
    assert run("status", "--target", str(t)).exit_code == 2
    r = run("intent", "init", "--target", str(t))
    assert r.exit_code == 0 and str(TargetPaths.from_root(t).intent) in r.output, r.output
    assert run("status", "--target", str(t)).exit_code == 0
    assert run("intent", "init", "--target", str(t)).exit_code == 0  # idempotent
    assert GitRepo(t).is_clean() and not (t / ".ateam").exists() and not (t / ".gitignore").exists()
    assert run("intent", "path", "--target", str(t)).output.strip() == str(TargetPaths.from_root(t).intent)


def test_intent_remote_push_pull(tmp_path, ateam_home):
    import subprocess
    t = _fresh_target(tmp_path, ateam_home)
    bare = tmp_path / "private.git"; subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    assert run("intent", "init", "--remote", str(bare), "--target", str(t)).exit_code == 0
    assert run("intent", "push", "--target", str(t)).exit_code == 0
    assert subprocess.run(["git", "log", "-1", "--format=%s", "main"], cwd=bare, text=True, capture_output=True).stdout.strip() == "ateam: initialise intent repo"
    assert run("intent", "pull", "--target", str(t)).exit_code == 0
    assert run("intent", "remote", str(bare), "--target", str(t)).exit_code == 0


def test_doctor_shows_id_source_and_intent_path(cli_target):
    r = run("doctor", "--target", str(cli_target))
    p = TargetPaths.from_root(cli_target)
    assert r.exit_code == 0 and "from path" in r.output and str(p.intent) in r.output and "WARN" not in r.output


def test_doctor_warns_when_id_source_changed(cli_target):
    run("doctor", "--target", str(cli_target))  # records the current derivation
    GitRepo(cli_target).run("remote", "add", "origin", "git@github.com:a/b.git")
    r = run("doctor", "--target", str(cli_target))
    assert "WARN" in r.output and "from origin" in r.output and "was path" in r.output


def test_worker_run_leaves_target_and_worktree_free_of_ateam_files(cli_target, tmp_path):
    _frozen_target(cli_target)
    wt = tmp_path / "wt"; GitRepo(cli_target).worktree_add(wt, "wt-free", "main")
    r = run("worker", "run", "C-0001", "--role", "investigate", "--cwd", str(wt), "--target", str(cli_target))
    assert r.exit_code == 1 and "no investigate tier" in r.output
    for root in (cli_target, wt):
        assert not (root / ".ateam").exists() and not (root / ".ateam-scratch").exists()
    assert GitRepo(cli_target).is_clean() and GitRepo(wt).is_clean()


# ---- review: freeze guard, origin-change adoption, doctor detail

def test_contracts_freeze_refuses_dirty_intent_and_commits_only_its_file(cli_target, sample_contract):
    from ateam.contracts.store import save_contract
    p = TargetPaths.from_root(cli_target)
    run("records", "approve", "R-0001", "--target", str(cli_target))
    (p.contracts / "stray.txt").write_text("x: 1\n")
    r = run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    assert r.exit_code == 1 and "uncommitted" in r.output and str(p.intent) in r.output
    (p.contracts / "stray.txt").unlink()
    save_contract(p.contracts, sample_contract.model_copy(update={"id": "C-0002", "title": "Second"}))
    commit_intent(p, "seed C-0002", ["contracts"])
    assert run("contracts", "freeze", "C-0001", "--target", str(cli_target)).exit_code == 0
    g = GitRepo(p.intent)
    assert g.is_clean() and g.run("log", "-1", "--format=%s").strip() == "ateam: freeze C-0001"
    assert g.run("show", "--stat", "--format=", "HEAD").count(".yaml") == 1


def test_intent_init_refuses_orphaning_after_origin_change_unless_adopt(cli_target):
    old = TargetPaths.from_root(cli_target)
    assert old.exists()
    GitRepo(cli_target).run("remote", "add", "origin", "git@github.com:a/b.git")
    new = TargetPaths.from_root(cli_target)
    assert new.intent != old.intent
    r = run("status", "--target", str(cli_target))
    assert r.exit_code == 2 and "--adopt" in r.output and old.intent.parent.name in r.output
    r2 = run("intent", "init", "--target", str(cli_target))
    assert r2.exit_code == 1 and "--adopt" in r2.output and old.intent.parent.name in r2.output and new.intent.parent.name in r2.output
    assert old.exists() and not new.exists()
    r3 = run("intent", "init", "--adopt", "--target", str(cli_target))
    assert r3.exit_code == 0, r3.output
    assert new.exists() and not old.intent.exists()
    assert (new.records / "R-0001-reading-state-is-only-ever-set.yaml").is_file()
    assert run("status", "--target", str(cli_target)).exit_code == 0


def test_doctor_names_both_ids_and_leaves_old_repo(cli_target):
    old = TargetPaths.from_root(cli_target)
    run("doctor", "--target", str(cli_target))
    GitRepo(cli_target).run("remote", "add", "origin", "git@github.com:a/b.git")
    new = TargetPaths.from_root(cli_target)
    r = run("doctor", "--target", str(cli_target))
    assert "WARN" in r.output and old.intent.parent.name in r.output and new.intent.parent.name in r.output and "--adopt" in r.output
    assert old.exists()
    r2 = run("doctor", "--target", str(cli_target))
    assert "WARN" in r2.output  # the warning persists until the owner resolves it


# ---- stage 1: ateam init and records review

def _canned_discovery(**over):
    report = {"candidates": [{
        "kind": "invariant", "statement": "Routes never read environment variables directly", "rationale": "testability",
        "origin": "documented", "basis": "requirement", "evidence": [{"type": "doc", "ref": "README.md#rules"}],
        "paths": ["src/api/**"], "serves": ["testable handlers"], "confidence": "high"}], "unknowns": ["which port"], "notes": "n"}
    report.update(over)
    return report


def _patch_investigation(monkeypatch, report):
    from ateam.runner import investigate as inv_mod
    from ateam.runner.run import WorkerResult

    async def fake(**kw):
        db = kw["db"]
        a = db.start_attempt(kw["name"], "investigate", "light", "haiku", {})
        db.finish_attempt(a, "completed", None, 1, 1, 0.001, 1, 0.1, "s")
        res = WorkerResult("success", False, "completed", "s", 1, 0.001, {}, {}, report, [], [], None, 0.1)
        return inv_mod.InvestigationResult(res=res, attempt_id=a, tampered=[])

    monkeypatch.setattr("ateam.cli.run_investigation", fake, raising=False)
    monkeypatch.setattr("ateam.discovery.run_investigation", fake, raising=False)
    monkeypatch.setattr(inv_mod, "run_investigation", fake)


def test_init_writes_proposed_records_and_commits(monkeypatch, target_repo, target_paths):
    (target_repo / "README.md").write_text("# rules\nRoutes never read env.\n")
    GitRepo(target_repo).run("add", "README.md"); GitRepo(target_repo).run("commit", "-q", "-m", "readme")
    _patch_investigation(monkeypatch, _canned_discovery())
    r = run("init", "--target", str(target_repo))
    assert r.exit_code == 0, r.output
    assert "R-0001" in r.output and "ateam records review" in r.output and "which port" in r.output
    recs = load_records(target_paths.records)
    assert recs["R-0001"].status == "proposed"
    g = GitRepo(target_paths.intent)
    assert g.is_clean() and "propose 1 record" in g.run("log", "-1", "--format=%s")
    assert GitRepo(target_repo).is_clean()


def test_init_with_zero_candidates_exits_zero(monkeypatch, target_repo, target_paths):
    _patch_investigation(monkeypatch, _canned_discovery(candidates=[]))
    r = run("init", "--target", str(target_repo))
    assert r.exit_code == 0 and "0 records" in r.output and load_records(target_paths.records) == {}


def test_init_creates_intent_repo_when_missing(monkeypatch, tmp_path, ateam_home):
    t = _fresh_target(tmp_path, ateam_home)
    _patch_investigation(monkeypatch, _canned_discovery(candidates=[]))
    assert run("init", "--target", str(t)).exit_code == 0 and TargetPaths.from_root(t).exists()


def test_records_review_approve_reject_skip_via_input(cli_target, sample_record):
    p = TargetPaths.from_root(cli_target)
    for rid in ("R-0002", "R-0003"):
        save_record(p.records, sample_record.model_copy(update={"id": rid, "statement": f"Rule {rid}"}))
    commit_intent(p, "seed", ["records"])
    r = run("records", "review", "--target", str(cli_target), input="r\nj\nnot needed\ns\n")
    assert r.exit_code == 0, r.output
    recs = load_records(p.records)
    assert recs["R-0001"].status == "approved" and recs["R-0001"].basis == "requirement"
    assert recs["R-0002"].status == "rejected" and recs["R-0002"].history[-1].why == "not needed"
    assert recs["R-0003"].status == "proposed"
    assert "approved 1" in r.output and "rejected 1" in r.output and "skipped 1" in r.output


# ---- stage 2: ateam plan

def _canned_plan():
    return {"title": "Health endpoint", "contracts": [{
        "title": "Add health", "paths": ["src/api/**", "tests/**"], "incidental": [], "depends_on": [],
        "acceptance": [{"id": "A1", "statement": "health() returns ok", "check": {"type": "test", "ref": "tests/test_routes.py::RoutesTest::test_health"}}],
        "checks": [], "allocation": {"complexity": "low", "risk": "low", "uncertainty": "low", "reasoning": "one function",
                                     "tier": {"build": "light", "test": "light", "investigate": "none"}}}],
        "proposed_records": [], "unknowns": ["which port"], "notes": "n"}


def test_plan_command_writes_plan_contracts_and_transitions(monkeypatch, cli_target):
    from ateam.plans.store import load_plans
    _patch_investigation(monkeypatch, _canned_plan())
    r = run("plan", "add a health function", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    p = TargetPaths.from_root(cli_target)
    plans = load_plans(p.plans)
    assert plans["P-0001"].status == "plan_proposed" and plans["P-0001"].contracts == ["C-0002"]   # C-0001 exists from the fixture
    assert load_contracts(p.contracts)["C-0002"].status == "draft"
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert db.feature_status("P-0001") == "plan_proposed"
    assert GitRepo(p.intent).is_clean() and "propose plan P-0001" in GitRepo(p.intent).run("log", "-1", "--format=%s")
    assert "ateam approve P-0001" in r.output and "which port" in r.output
    assert run("plans", "list", "--target", str(cli_target)).output.count("P-0001") == 1
    assert "add a health function" in run("plans", "show", "P-0001", "--target", str(cli_target)).output


def test_plan_command_invalid_report_rejects_and_writes_nothing(monkeypatch, cli_target):
    from ateam.plans.store import load_plans
    bad = _canned_plan(); bad["contracts"][0]["depends_on"] = [0]
    _patch_investigation(monkeypatch, bad)
    p = TargetPaths.from_root(cli_target)
    r = run("plan", "add a health function", "--target", str(cli_target))
    assert r.exit_code == 1 and "cycle" in r.output
    # no contract and no proposed record is written; the plan itself is recorded as rejected so the id is not reused
    assert "C-0002" not in load_contracts(p.contracts) and load_plans(p.plans)["P-0001"].status == "plan_rejected"
    assert load_plans(p.plans)["P-0001"].contracts == [] and GitRepo(p.intent).is_clean()
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert db.feature_status("P-0001") == "plan_rejected" and db.contract_hash("C-0002") is None


# ---- stage 2: approve and reject a plan

def _proposed_plan(monkeypatch, cli_target, two=False, with_record=False):
    rep = _canned_plan()
    if two:
        second = dict(rep["contracts"][0]); second = {**second, "title": "Use health in router", "depends_on": [0],
                                                    "allocation": {**second["allocation"], "complexity": "high", "tier": {"build": "careful", "test": "light", "investigate": "none"}}}
        rep["contracts"].append(second)
    if with_record:
        rep["proposed_records"] = [{"kind": "convention", "statement": "Health endpoints return plain strings", "rationale": "simple", "origin": "inferred",
                                    "basis": "observation", "evidence": [{"type": "code", "ref": "src/api/x.py:1"}], "paths": ["src/api/**"], "serves": [], "confidence": "medium"}]
    _patch_investigation(monkeypatch, rep)
    assert run("records", "approve", "R-0001", "--target", str(cli_target)).exit_code == 0   # so src/api/** has a binding record
    assert run("plan", "add a health function", "--target", str(cli_target)).exit_code == 0
    return TargetPaths.from_root(cli_target)


def test_approve_freezes_contracts_in_dependency_order_and_transitions(monkeypatch, cli_target):
    p = _proposed_plan(monkeypatch, cli_target, two=True)
    r = run("approve", "P-0001", "--skip-records", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    cs = load_contracts(p.contracts)
    assert cs["C-0002"].status == "ready" and cs["C-0002"].records and cs["C-0003"].status == "draft"   # C-0003 depends on C-0002
    assert r.output.index("C-0002") < r.output.index("C-0003")
    from ateam.plans.store import load_plans
    assert load_plans(p.plans)["P-0001"].status == "plan_approved"
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert db.feature_status("P-0001") == "plan_approved" and db.contract_status("C-0002") == "ready" and db.contract_hash("C-0003")
    g = GitRepo(p.intent)
    assert g.is_clean() and "approve plan P-0001" in g.run("log", "-1", "--format=%s")


def test_approve_applies_tier_override_and_raises_test_tier(monkeypatch, cli_target):
    p = _proposed_plan(monkeypatch, cli_target, two=True)
    r = run("approve", "P-0001", "--skip-records", "--tier", "C-0002=careful", "--tier", "C-0003:test=light", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    cs = load_contracts(p.contracts)
    assert cs["C-0002"].allocation.tier == {"build": "careful", "test": "careful", "investigate": "none"}
    assert cs["C-0003"].allocation.tier["test"] == "careful" and "raised" in r.output   # build is careful; test cannot go below it
    from ateam.plans.store import load_plans
    assert load_plans(p.plans)["P-0001"].tier_overrides == {"C-0002": {"build": "careful", "test": "careful"}, "C-0003": {"test": "light"}}


def test_approve_requires_plan_proposed(monkeypatch, cli_target):
    _proposed_plan(monkeypatch, cli_target)
    assert run("approve", "P-0001", "--skip-records", "--target", str(cli_target)).exit_code == 0
    r = run("approve", "P-0001", "--skip-records", "--target", str(cli_target))
    assert r.exit_code == 1 and "plan_approved" in r.output


def test_approve_runs_records_review_unless_skipped(monkeypatch, cli_target):
    p = _proposed_plan(monkeypatch, cli_target, with_record=True)
    rid = load_plans_ids(p)[0]
    r = run("approve", "P-0001", "--target", str(cli_target), input="o\n")
    assert r.exit_code == 0, r.output
    assert load_records(p.records)[rid].status == "approved" and load_records(p.records)[rid].basis == "observation"


def load_plans_ids(p):
    from ateam.plans.store import load_plans
    return load_plans(p.plans)["P-0001"].proposed_records


def test_reject_plan_records_reason_and_leaves_contracts_draft(monkeypatch, cli_target):
    p = _proposed_plan(monkeypatch, cli_target)
    r = run("reject", "P-0001", "too broad", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    from ateam.plans.store import load_plans
    plan = load_plans(p.plans)["P-0001"]
    assert plan.status == "plan_rejected" and plan.history[-1].why == "too broad"
    assert load_contracts(p.contracts)["C-0002"].status == "draft"
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert db.feature_status("P-0001") == "plan_rejected" and db.contract_hash("C-0002") is None
    assert "reject plan P-0001" in GitRepo(p.intent).run("log", "-1", "--format=%s")


def test_status_lists_plans(monkeypatch, cli_target):
    _proposed_plan(monkeypatch, cli_target)
    out = run("status", "--target", str(cli_target)).output
    assert "P-0001" in out and "plan_proposed" in out and "1 contract" in out



# ---- review round: plan retry after failure, approve commit failure, records review --all

def test_plan_can_be_rerun_after_an_invalid_report(monkeypatch, cli_target):
    from ateam.plans.store import load_plans
    bad = _canned_plan(); bad["contracts"][0]["depends_on"] = [0]
    _patch_investigation(monkeypatch, bad)
    assert run("plan", "add a health function", "--target", str(cli_target)).exit_code == 1
    p = TargetPaths.from_root(cli_target)
    plans = load_plans(p.plans)
    assert plans["P-0001"].status == "plan_rejected"          # the failure is visible as a plan file
    assert "P-0001" in run("plans", "list", "--target", str(cli_target)).output
    _patch_investigation(monkeypatch, _canned_plan())
    r = run("plan", "add a health function", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    assert load_plans(p.plans)["P-0002"].status == "plan_proposed"


def test_plan_crash_in_investigation_leaves_terminal_state_and_rerun_works(monkeypatch, cli_target):
    from ateam.plans.store import load_plans
    from ateam.runner import investigate as inv_mod

    async def boom(**kw):
        raise RuntimeError("sdk exploded")

    monkeypatch.setattr("ateam.cli.run_investigation", boom)
    r = run("plan", "add a health function", "--target", str(cli_target))
    assert r.exit_code == 1 and "sdk exploded" in r.output and "Traceback" not in r.output
    p = TargetPaths.from_root(cli_target)
    assert load_plans(p.plans)["P-0001"].status == "plan_rejected"
    _patch_investigation(monkeypatch, _canned_plan())
    assert run("plan", "add a health function", "--target", str(cli_target)).exit_code == 0


def test_approve_commit_failure_prints_recovery_and_leaves_store_untouched(monkeypatch, cli_target):
    p = _proposed_plan(monkeypatch, cli_target)
    from ateam.intent import IntentError

    def fail_commit(paths, message, subpaths):
        raise IntentError("simulated commit failure")

    # `commit_intent` must stay a module-level name in ateam.cli for this patch to bite; approve calls it through the module global.
    monkeypatch.setattr("ateam.cli.commit_intent", fail_commit)
    r = run("approve", "P-0001", "--skip-records", "--target", str(cli_target))
    assert r.exit_code == 1 and "git -C" in r.output and "checkout" in r.output
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert db.feature_status("P-0001") == "plan_proposed" and db.contract_hash("C-0002") is None
    # the printed recovery really restores a clean intent repo
    import re, subprocess
    cmd = re.search(r"^\s*(git -C .*)$", r.output, re.M).group(1)
    subprocess.run(cmd, shell=True, check=True)
    assert GitRepo(p.intent).is_clean()


def test_records_review_all_shows_non_proposed_without_prompting(cli_target):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    r = run("records", "review", "--all", "--target", str(cli_target), input="")
    assert r.exit_code == 0 and "R-0001" in r.output and "approved" in r.output and "nothing to decide" in r.output.lower()
    r2 = run("records", "review", "--target", str(cli_target), input="")
    assert "nothing proposed" in r2.output.lower()



def test_plan_id_allocation_uses_state_store_when_plan_file_is_missing(monkeypatch, cli_target):
    from ateam.plans.store import load_plans
    bad = _canned_plan(); bad["contracts"][0]["depends_on"] = [0]
    _patch_investigation(monkeypatch, bad)
    assert run("plan", "x", "--target", str(cli_target)).exit_code == 1
    p = TargetPaths.from_root(cli_target)
    for f in p.plans.glob("P-0001-*.yaml"):
        f.unlink()
    commit_intent(p, "owner deleted the rejected plan file", ["plans"])
    _patch_investigation(monkeypatch, _canned_plan())
    assert run("plan", "x", "--target", str(cli_target)).exit_code == 0
    assert "P-0002" in load_plans(p.plans) and "P-0001" not in load_plans(p.plans)


def test_approve_notes_overrides_below_rubric_floor(monkeypatch, cli_target):
    _proposed_plan(monkeypatch, cli_target, two=True)   # C-0003 has complexity high -> floor careful
    r = run("approve", "P-0001", "--skip-records", "--tier", "C-0003=light", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    assert "below the rubric" in r.output and "C-0003" in r.output


# ---------------------------------------------------------------- investigation brief wiring


def _capture_investigation(monkeypatch, report):
    from ateam.runner import investigate as inv_mod
    from ateam.runner.run import WorkerResult

    seen: dict = {}

    async def fake(**kw):
        seen.update(kw)
        db = kw["db"]
        a = db.start_attempt(kw["name"], "investigate", "light", "haiku", {})
        db.finish_attempt(a, "completed", None, 1, 1, 0.001, 1, 0.1, "s")
        res = WorkerResult("success", False, "completed", "s", 1, 0.001, {}, {}, report, [], [], None, 0.1)
        return inv_mod.InvestigationResult(res=res, attempt_id=a, tampered=[])

    monkeypatch.setattr("ateam.cli.run_investigation", fake, raising=False)
    return seen


def _section(brief: str, heading: str) -> str:
    after = brief.split(f"## {heading}\n", 1)[1]
    return after.split("\n## ", 1)[0]


def test_plan_brief_contains_rules_records_and_plan_index(monkeypatch, cli_target):
    from ateam.plans.model import Plan
    from ateam.plans.store import save_plan

    (cli_target / "CLAUDE.md").write_text("Never touch the vault.\n")
    p = TargetPaths.from_root(cli_target)
    save_plan(p.plans, Plan(id="P-0001", title="Earlier plan", request="r", contracts=["C-0001"]))
    commit_intent(p, "seed plan", ["plans"])
    seen = _capture_investigation(monkeypatch, _canned_plan())
    r = run("plan", "add a health function", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    brief = seen["brief_text"]
    assert "Never touch the vault." in _section(brief, "Rules")
    assert "R-0001" in _section(brief, "Records").split("Proposed, not yet approved")[1]
    index = _section(brief, "Existing plans and contracts")
    assert "P-0001" in index and "C-0001" in index
    assert "add a health function" in _section(brief, "Request")
    assert str(p.scratch_root / "P-0002" / "handoff.yaml") in _section(brief, "Handoff file")
    assert "files_read" in seen["prompt"] and "Read the Rules section first" in seen["prompt"]


def test_init_brief_has_discovery_limits_and_rules(monkeypatch, cli_target):
    (cli_target / "CLAUDE.md").write_text("Routes never read env.\n")
    seen = _capture_investigation(monkeypatch, _canned_discovery())
    r = run("init", "--max-candidates", "5", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    brief = seen["brief_text"]
    assert "Routes never read env." in _section(brief, "Rules")
    assert "at most 5 candidates" in _section(brief, "Discovery limits")
    assert str(TargetPaths.from_root(cli_target).scratch_root / "init" / "handoff.yaml") in brief
    assert seen["prompt"].startswith("Discover this repository's existing engineering intent. Read the Rules section")


def test_plan_stores_new_fields_and_review_prints_them(monkeypatch, cli_target):
    from ateam.plans.store import load_plans

    rep = _canned_plan()
    rep.update(files_read=["src/app.py"], out_of_scope=["auth"], verification_approach="run the health test",
               depends_on_plans=[])
    _capture_investigation(monkeypatch, rep)
    r = run("plan", "add a health function", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    assert "run the health test" in r.output
    stored = load_plans(TargetPaths.from_root(cli_target).plans)["P-0001"]
    assert stored.files_read == ["src/app.py"] and stored.out_of_scope == ["auth"]
    assert "verification_approach: run the health test" in run("plans", "show", "P-0001", "--target", str(cli_target)).output


def test_plan_missing_budget_uses_tier(monkeypatch, cli_target):
    from ateam.allocation import budget_for
    from ateam.tiers import load_tiers

    _capture_investigation(monkeypatch, _canned_plan())
    r = run("plan", "add a health function", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    c = load_contracts(TargetPaths.from_root(cli_target).contracts)["C-0002"]
    assert c.budget == budget_for("light", load_tiers())
