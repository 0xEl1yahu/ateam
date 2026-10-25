import pytest

from ateam.state.db import CONTRACT_TRANSITIONS, FEATURE_TRANSITIONS, InvalidTransition, StateDB


def fresh(tmp_path):
    db = StateDB(tmp_path / "s.db")
    db.init_schema()
    return db


def test_init_schema_idempotent(tmp_path):
    db = fresh(tmp_path)
    db.init_schema()
    assert db.schema_version() == 1


def test_transition_tables_match_design():
    assert FEATURE_TRANSITIONS["plan_proposed"] == {"plan_approved", "plan_rejected"}
    assert CONTRACT_TRANSITIONS["verified"] == {"integrated"}
    assert "integrated" not in CONTRACT_TRANSITIONS["ready"]


def test_contract_transition_rules(tmp_path):
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "draft", "h")
    db.transition("contract", "C-0001", "ready", by="ateam")
    with pytest.raises(InvalidTransition):
        db.transition("contract", "C-0001", "integrated", by="ateam")
    assert db.contract_status("C-0001") == "ready"
    rows = db.transitions("contract", "C-0001")
    assert rows[-1]["from_status"] == "draft" and rows[-1]["to_status"] == "ready" and rows[-1]["by"] == "ateam"


def test_record_hashes(tmp_path):
    db = fresh(tmp_path)
    db.set_record_hash("R-0001", "abc"); db.set_record_hash("R-0001", "def")
    assert db.record_hashes() == {"R-0001": "def"}


def test_feature_starts_at_requested(tmp_path):
    db = fresh(tmp_path)
    with pytest.raises(InvalidTransition):
        db.transition("feature", "P-0001", "executing", by="ateam")
    db.transition("feature", "P-0001", "requested", by="owner")
    db.transition("feature", "P-0001", "investigating", by="ateam")
    assert db.feature_status("P-0001") == "investigating"


def test_pause_reason_is_kept(tmp_path):
    db = fresh(tmp_path)
    for s in ("requested", "investigating", "plan_proposed", "plan_approved", "executing"):
        db.transition("feature", "P-0001", s, by="x")
    db.transition("feature", "P-0001", "paused", by="ateam", reason="budget_exhausted")
    assert db.transitions("feature", "P-0001")[-1]["reason"] == "budget_exhausted"


def test_attempt_and_telemetry(tmp_path):
    db = fresh(tmp_path)
    a = db.start_attempt("C-0001", "build", "standard", "sonnet", {"complexity": "medium", "risk": "high", "uncertainty": "low"})
    db.finish_attempt(a, "verified", None, 1000, 200, 0.05, 12, 30.5, "sess")
    row = db.telemetry_rows()[0]
    assert row["contract_id"] == "C-0001" and row["role"] == "build" and row["predicted_risk"] == "high"
    assert row["cost_usd"] == 0.05 and row["outcome"] == "verified" and row["session_id"] == "sess" and row["failure_class"] is None


def test_checkpoint_and_lock(tmp_path):
    db = fresh(tmp_path)
    assert db.latest_checkpoint("C-0001") is None
    db.set_checkpoint("C-0001", 1, {"head": "abc"}); db.set_checkpoint("C-0001", 2, {"head": "def"})
    assert db.latest_checkpoint("C-0001") == {"head": "def"}
    assert db.acquire_run_lock("P-0001") and not db.acquire_run_lock("P-0002")
    assert db.acquire_run_lock("P-0001")  # re-entrant for the holder
    db.release_run_lock("P-0001")
    assert db.acquire_run_lock("P-0002")


def test_run_lock_is_exclusive_across_connections(tmp_path):
    a = fresh(tmp_path)
    b = StateDB(tmp_path / "s.db")
    assert a.acquire_run_lock("P-0001")
    assert not b.acquire_run_lock("P-0002")
    assert b.acquire_run_lock("P-0001")  # same holder, re-entrant from any connection
    assert a.run_lock_holder() == "P-0001"
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):  # the schema itself forbids a second holder, so a race cannot create one
        b.conn.execute("INSERT INTO run_lock (singleton, plan_id, acquired_at) VALUES (1, 'P-0002', 'now')")
    b.conn.rollback()
    a.release_run_lock("P-0001")
    assert b.acquire_run_lock("P-0002") and a.run_lock_holder() == "P-0002"


def test_meta_key_value(tmp_path):
    db = fresh(tmp_path)
    assert db.get_meta("repo_id_source") is None
    db.set_meta("repo_id_source", "path"); db.set_meta("repo_id_source", "origin")
    assert db.get_meta("repo_id_source") == "origin"
