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
    assert FEATURE_TRANSITIONS["plan_proposed"] == {"plan_approved", "plan_rejected", "superseded"}
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


def test_finish_verification_writes_walk_checkpoint_and_clears_the_marker_together(tmp_path):
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "ready", "h")
    db.set_meta("verify-pending:C-0001", "{}")
    db.finish_verification("C-0001", "ready", ["building", "verifying", "verified"], by="verify", reason="r",
                           checkpoint={"k": 1}, clear_keys=["verify-pending:C-0001", "verify-running:C-0001"])
    assert db.contract_status("C-0001") == "verified"
    rows = db.transitions("contract", "C-0001")
    assert [(r["from_status"], r["to_status"]) for r in rows] == [("ready", "building"), ("building", "verifying"), ("verifying", "verified")]
    assert {r["by"] for r in rows} == {"verify"} and {r["reason"] for r in rows} == {"r"}
    assert db.latest_checkpoint("C-0001") == {"k": 1} and db.get_meta("verify-pending:C-0001") is None


def test_finish_verification_with_an_invalid_step_writes_nothing(tmp_path):
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "ready", "h")
    db.set_meta("verify-pending:C-0001", "{}")
    with pytest.raises(InvalidTransition):
        db.finish_verification("C-0001", "ready", ["building", "verified"], by="verify", reason="r",
                               checkpoint={"k": 1}, clear_keys=["verify-pending:C-0001"])
    assert db.contract_status("C-0001") == "ready" and db.transitions("contract", "C-0001") == []
    assert db.latest_checkpoint("C-0001") is None and db.get_meta("verify-pending:C-0001") == "{}"


def test_finish_verification_walks_from_the_status_seen_before_the_checks(tmp_path):
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "verified", "h")   # rewritten behind the verifier's back
    db.finish_verification("C-0001", "ready", ["building", "verifying", "failed"], by="verify", reason="tamper",
                           checkpoint={}, clear_keys=[])
    assert db.contract_status("C-0001") == "failed"
    assert db.transitions("contract", "C-0001")[0]["from_status"] == "ready"


def test_fingerprint_sees_writes_from_another_connection(tmp_path):
    import sqlite3
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "ready", "h")
    first = db.fingerprint("C-0001")
    assert db.fingerprint("C-0001") == first
    other = sqlite3.connect(tmp_path / "s.db")
    with other:
        other.execute("UPDATE contracts SET status='verified'")
    assert db.fingerprint("C-0001") != first


def test_fingerprint_is_scoped_to_one_contract_plus_record_hashes(tmp_path):
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "ready", "h")
    db.upsert_contract("C-0002", "P-0001", "ready", "h")
    first = db.fingerprint("C-0001")
    db.set_meta("unrelated", "x")
    db.transition("contract", "C-0002", "building", by="worker")
    a = db.start_attempt("C-0002", "build", "light", "haiku", {})
    db.set_checkpoint("C-0002", a, {"x": 1})
    db.set_meta("verify-running:C-0002", "{}")
    assert db.fingerprint("C-0001") == first
    for change in (lambda: db.set_meta("verify-pending:C-0001", "{}"), lambda: db.set_record_hash("R-0001", "z"),
                   lambda: db.set_checkpoint("C-0001", None, {}), lambda: db.start_attempt("C-0001", "test", "light", "haiku", {})):
        change()
        assert db.fingerprint("C-0001") != first
        first = db.fingerprint("C-0001")


def test_delete_meta(tmp_path):
    db = fresh(tmp_path)
    db.set_meta("k", "v"); db.set_meta("j", "w"); db.delete_meta("k", "j", "absent")
    assert db.get_meta("k") is None and db.get_meta("j") is None


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


def test_feature_transitions_include_covered_and_superseded():
    assert "plan_covered" in FEATURE_TRANSITIONS["investigating"] and "superseded" in FEATURE_TRANSITIONS["plan_proposed"]
    assert FEATURE_TRANSITIONS["plan_covered"] == set() == FEATURE_TRANSITIONS["superseded"]
