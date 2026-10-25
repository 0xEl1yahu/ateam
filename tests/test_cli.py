from click.testing import CliRunner

from ateam.cli import main


def test_version_flag():
    r = CliRunner().invoke(main, ["--version"])
    assert r.exit_code == 0
    assert "0.1.0" in r.output


import yaml

from ateam.contracts.store import load_contracts
from ateam.gitops import GitRepo
from ateam.paths import home_dir
from ateam.records.store import load_records, save_record
from ateam.state.db import StateDB


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_records_validate_reports_errors(cli_target, sample_record):
    bad = sample_record.model_copy(update={"id": "R-0002"})
    bad.binds.paths = ["./src/**"]
    save_record(cli_target / ".ateam/records", bad)
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
    assert GitRepo(cli_target).run("log", "-1", "--format=%s").strip() == "ateam: approve R-0001"
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert "R-0001" in db.record_hashes()


def test_records_approve_shows_overlaps(cli_target, sample_record):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    second = sample_record.model_copy(update={"id": "R-0002", "statement": "Books API returns JSON only"})
    save_record(cli_target / ".ateam/records", second)
    GitRepo(cli_target).commit_paths([".ateam"], "seed 2")
    r = run("records", "approve", "R-0002", "--target", str(cli_target))
    assert r.exit_code == 0, r.output
    assert "Overlapping approved records:" in r.output and "R-0001" in r.output


def test_records_reject_and_guard_message(cli_target):
    r = run("records", "reject", "R-0001", "covered elsewhere", "--target", str(cli_target))
    assert r.exit_code == 0 and load_records(cli_target / ".ateam/records")["R-0001"].status == "rejected"
    r2 = run("records", "approve", "R-0001", "--target", str(cli_target))
    assert r2.exit_code == 1 and "expected proposed" in r2.output


def test_contracts_show_and_freeze_selects_records(cli_target):
    run("records", "approve", "R-0001", "--target", str(cli_target))
    assert "Expose reading state" in run("contracts", "show", "C-0001", "--target", str(cli_target)).output
    r = run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    assert r.exit_code == 0 and "R-0001" in r.output, r.output
    c = load_contracts(cli_target / ".ateam/contracts")["C-0001"]
    assert c.records[0].id == "R-0001" and c.records[0].hash and c.status == "ready"
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    assert db.contract_status("C-0001") == "ready"


def test_status_without_ateam_dir(tmp_path, ateam_home):
    r = run("status", "--target", str(tmp_path))
    assert r.exit_code == 2 and "No .ateam/ directory" in r.output and "see README" in r.output


def test_status_lists_contracts(cli_target):
    run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    r = run("status", "--target", str(cli_target))
    assert r.exit_code == 0 and "C-0001" in r.output and "ready" in r.output


def test_doctor_mentions_versions(cli_target):
    r = run("doctor", "--target", str(cli_target))
    assert r.exit_code == 0 and "SDK 0.2." in r.output and "2.1.310" in r.output and "main" in r.output


def test_records_validate_reports_malformed_file_without_traceback(cli_target):
    (cli_target / ".ateam/records/notes.yaml").write_text("just: notes\n")
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
    c = load_contracts(cli_target / ".ateam/contracts")["C-0001"]
    from ateam.contracts.store import save_contract
    save_contract(cli_target / ".ateam/contracts", c.model_copy(update={"title": "edited by hand"}))
    r = run("contracts", "validate", "--target", str(cli_target))
    assert r.exit_code == 1 and "C-0001" in r.output and "hand edit" in r.output
    save_contract(cli_target / ".ateam/contracts", c)
    save_record(cli_target / ".ateam/records", load_records(cli_target / ".ateam/records")["R-0001"].model_copy(update={"statement": "changed"}))
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
    (cli_target / ".ateam/records/notes.yaml").write_text("just: notes\n")
    for args in (["records", "list"], ["records", "show", "R-0001"], ["records", "approve", "R-0001"], ["contracts", "freeze", "C-0001"], ["brief", "C-0001", "--role", "build"]):
        r = run(*args, "--target", str(cli_target))
        assert r.exit_code == 1 and "notes.yaml" in r.output and "Traceback" not in r.output, args
