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
    assert r.exit_code == 2 and "Run: ateam init" in r.output


def test_status_lists_contracts(cli_target):
    run("contracts", "freeze", "C-0001", "--target", str(cli_target))
    r = run("status", "--target", str(cli_target))
    assert r.exit_code == 0 and "C-0001" in r.output and "ready" in r.output


def test_doctor_mentions_versions(cli_target):
    r = run("doctor", "--target", str(cli_target))
    assert r.exit_code == 0 and "SDK 0.2." in r.output and "2.1.310" in r.output and "main" in r.output
