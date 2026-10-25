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
