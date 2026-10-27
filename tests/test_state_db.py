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
    assert CONTRACT_TRANSITIONS["verified"] == {"integrated", "building"}   # building: the rebase BUILD only (owner, 2026-10-27)
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
    from contextlib import closing
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "ready", "h")
    first = db.fingerprint("C-0001")
    assert db.fingerprint("C-0001") == first
    with closing(sqlite3.connect(tmp_path / "s.db")) as other, other:
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


# Owner, 2026-10-26: an approved plan that has not started can be superseded, and so can its draft and ready contracts.
def test_approved_feature_can_be_superseded_and_superseded_is_terminal():
    assert FEATURE_TRANSITIONS["plan_approved"] == {"executing", "superseded", "plan_rejected"}
    assert CONTRACT_TRANSITIONS["draft"] == {"ready", "superseded"} and CONTRACT_TRANSITIONS["ready"] == {"building", "superseded"}
    assert CONTRACT_TRANSITIONS["superseded"] == set()
    for started in ("building", "verifying", "failed", "paused", "verified", "integrated", None):
        assert "superseded" not in CONTRACT_TRANSITIONS[started]


def test_every_contract_status_has_a_transition_row():
    from ateam.contracts.model import ContractStatus
    assert {str(s) for s in ContractStatus} == {k for k in CONTRACT_TRANSITIONS if k is not None}


def _approved(db, plan="P-0001"):
    for to in ("requested", "investigating", "plan_proposed", "plan_approved"):
        db.transition("feature", plan, to, by="t")
    db.upsert_contract("C-0002", plan, "ready", "h2")
    db.upsert_contract("C-0003", plan, "draft", "h3")


def test_supersede_writes_every_transition_in_one_go(tmp_path):
    db = fresh(tmp_path)
    _approved(db)
    db.supersede("P-0001", "plan_approved", {"C-0002": "ready", "C-0003": "draft"}, by="owner", reason="replaced by P-0002")
    assert db.feature_status("P-0001") == "superseded"
    assert db.contract_status("C-0002") == "superseded" == db.contract_status("C-0003")
    assert [t["from_status"] for t in db.transitions("contract", "C-0002")] == ["ready"]
    assert db.transitions("feature", "P-0001")[-1]["reason"] == "replaced by P-0002"


@pytest.mark.parametrize("steps", [
    {"C-0002": "ready", "C-0003": "ready"},      # C-0003 is draft in the store: the expected status is stale
    {"C-0002": "ready", "C-0009": "draft"},      # no store row
])
def test_supersede_writes_nothing_when_a_step_is_invalid(steps, tmp_path):
    db = fresh(tmp_path)
    _approved(db)
    with pytest.raises(InvalidTransition):
        db.supersede("P-0001", "plan_approved", steps, by="owner", reason="r")
    assert db.feature_status("P-0001") == "plan_approved" and db.contract_status("C-0002") == "ready"
    assert db.transitions("contract", "C-0002") == []


def test_supersede_refuses_a_contract_that_has_started(tmp_path):
    db = fresh(tmp_path)
    _approved(db)
    db.transition("contract", "C-0002", "building", by="t")
    with pytest.raises(InvalidTransition):
        db.supersede("P-0001", "plan_approved", {"C-0002": "building"}, by="owner", reason="r")
    assert db.feature_status("P-0001") == "plan_approved" and db.contract_status("C-0002") == "building"


def test_work_traces_names_every_kind_of_trace(tmp_path):
    db = fresh(tmp_path)
    _approved(db)
    assert db.work_traces("C-0002") == []
    db.start_attempt("C-0002", "build", "light", "haiku", {})
    db.transition("contract", "C-0002", "building", by="t")
    db.set_checkpoint("C-0002", None, {"x": 1})
    db.set_meta("verify-running:C-0002", "{}")
    assert db.work_traces("C-0002") == ["an attempt", "a contract transition", "a checkpoint", "a verify marker"]
    db.set_meta("verify-pending:C-0003", "{}")
    assert db.work_traces("C-0003") == ["a verify marker"]


# Owner, 2026-10-26: a failed integration pauses; a stale proposal goes back to integrating.
def test_integration_edges():
    assert FEATURE_TRANSITIONS["integrating"] == {"merge_proposed", "paused"}
    assert FEATURE_TRANSITIONS["merge_proposed"] == {"merged", "plan_rejected", "integrating"}


# Owner, 2026-10-26: an approved plan that has not started can be rejected; its contracts are superseded with it.
def test_reject_unstarted_approved_plan_edge_and_retire(tmp_path):
    assert FEATURE_TRANSITIONS["plan_approved"] == {"executing", "superseded", "plan_rejected"}
    db = fresh(tmp_path)
    _approved(db)
    db.retire("P-0001", "plan_approved", {"C-0002": "ready", "C-0003": "draft"}, by="owner", reason="start fresh", to="plan_rejected")
    assert db.feature_status("P-0001") == "plan_rejected"
    assert db.contract_status("C-0002") == "superseded" == db.contract_status("C-0003")
    assert db.transitions("feature", "P-0001")[-1]["reason"] == "start fresh"


def test_investigating_can_be_rejected_and_features_are_listed_by_status(tmp_path):
    db = fresh(tmp_path)
    assert "plan_rejected" in FEATURE_TRANSITIONS["investigating"]
    for fid in ("P-0001", "P-0002"):
        db.transition("feature", fid, "requested", by="t"); db.transition("feature", fid, "investigating", by="t")
    db.transition("feature", "P-0002", "plan_rejected", by="t", reason="interrupted")
    assert db.features_with_status("investigating") == ["P-0001"] and db.feature_status("P-0002") == "plan_rejected"


# ---- scheduler support (plan 2026-10-26-scheduler, task 1)

@pytest.mark.parametrize("pid", [1, None])
def test_run_lock_is_process_exclusive(pid, tmp_path):
    import os
    db = fresh(tmp_path)
    with db.conn:
        db.conn.execute("INSERT INTO run_lock (singleton, plan_id, acquired_at, pid) VALUES (1, 'P-0001', 'then', ?)", (pid,))
    assert not db.acquire_run_lock("P-0001")          # same plan, another (or unknown) process: never stolen
    assert db.run_lock_info() == {"plan_id": "P-0001", "pid": pid, "acquired_at": "then"}
    db.release_run_lock("P-0001")
    assert db.run_lock_info() is None
    assert db.acquire_run_lock("P-0001") and db.run_lock_info()["pid"] == os.getpid()


def test_open_attempts_are_closed_as_interrupted(tmp_path):
    db = fresh(tmp_path)
    a = db.start_attempt("C-0002", "build", "light", "haiku", {})
    b = db.start_attempt("C-0003", "test", "light", "haiku", {})
    db.finish_attempt(b, "completed", None, 1, 1, 0.1, 1, 1.0, "s")
    c = db.start_attempt("C-0009", "build", "light", "haiku", {})
    assert [r["attempt_id"] for r in db.open_attempts(["C-0002", "C-0003"])] == [a]
    db.interrupt_attempt(a); db.interrupt_attempt(b)
    rows = {r["attempt_id"]: r for r in db.telemetry_rows()}
    assert rows[a]["outcome"] == "interrupted" and rows[a]["failure_class"] is None and rows[a]["finished_at"]
    assert rows[b]["outcome"] == "completed" and rows[c]["finished_at"] is None


def test_attempts_for_filters_by_role(tmp_path):
    db = fresh(tmp_path)
    for role in ("build", "test", "build"):
        db.start_attempt("C-0002", role, "light", "haiku", {})
    db.start_attempt("C-0003", "build", "light", "haiku", {})
    assert len(db.attempts_for("C-0002")) == 3
    assert [r["role"] for r in db.attempts_for("C-0002", role="build")] == ["build", "build"]


def test_latest_checkpoint_by_role(tmp_path):
    db = fresh(tmp_path)
    b = db.start_attempt("C-0002", "build", "light", "haiku", {})
    t = db.start_attempt("C-0002", "test", "light", "haiku", {})
    db.set_checkpoint("C-0002", b, {"notes": "build"})
    db.set_checkpoint("C-0002", t, {"notes": None})
    db.set_checkpoint("C-0002", None, {"verify": True})
    assert db.latest_checkpoint("C-0002") == {"verify": True}
    assert db.latest_checkpoint("C-0002", role="build") == {"notes": "build"}
    assert db.latest_checkpoint("C-0002", role="test") == {"notes": None}


def test_busy_timeout_is_set(tmp_path):
    assert fresh(tmp_path).conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000


def test_pause_record_round_trip(tmp_path):
    db = fresh(tmp_path)
    pid = db.record_pause("P-0001", "budget_exhausted", "C-0002 has used all 2 BUILD attempts",
                          [{"contract_id": "C-0002", "reason": "budget_exhausted", "detail": "d", "contract_hash": "h1",
                            "evidence": {"code": 1}}])
    p = db.open_pause("P-0001")
    assert p["pause_id"] == pid and p["conflict"] is False and p["contracts"][0]["evidence"] == {"code": 1}
    db.set_pause_findings(pid, "C-0002", 7, {"summary": "s"}, None)
    assert db.open_pause("P-0001")["contracts"][0]["findings"] == {"summary": "s"}
    db.resume_pause(pid, ["C-0002"])
    assert db.open_pause("P-0001") is None


def test_window_starts_after_the_newest_attempt(tmp_path):
    db = fresh(tmp_path)
    assert db.window_start("C-0002") == 0
    a = db.start_attempt("C-0002", "build", "light", "haiku", {})
    db.start_attempt("C-0003", "build", "light", "haiku", {})
    db.resume_pause(None, ["C-0002"])
    assert db.window_start("C-0002") == a + 1 and db.window_start("C-0003") == 0


def test_stop_request_flag(tmp_path):
    db = fresh(tmp_path)
    assert not db.stop_requested("P-0001")
    db.request_stop("P-0001"); assert db.stop_requested("P-0001")
    db.clear_stop("P-0001"); assert not db.stop_requested("P-0001")


def test_existing_store_gains_the_pause_tables(tmp_path):
    db = fresh(tmp_path)
    with db.conn:
        for t in ("pauses", "pause_contracts", "attempt_windows"):
            db.conn.execute(f"DROP TABLE {t}")
    db.init_schema()
    assert db.open_pause("P-0001") is None and db.window_start("C-0001") == 0


def test_verified_may_go_back_to_building_for_a_rebase(tmp_path):
    db = fresh(tmp_path)
    db.upsert_contract("C-0001", "P-0001", "verified", "h")
    with pytest.raises(InvalidTransition):
        db.transition("contract", "C-0001", "failed", by="run")
    db.transition("contract", "C-0001", "building", by="run", reason="rebase onto abc")
    assert db.contract_status("C-0001") == "building"


def test_close_and_context_manager(tmp_path):
    import sqlite3

    db = fresh(tmp_path)
    db.close()
    db.close()   # a second close is a no-op
    with pytest.raises(sqlite3.ProgrammingError):
        db.schema_version()
    with StateDB(tmp_path / "s.db") as inner:
        assert isinstance(inner, StateDB)
        assert inner.schema_version() == 1
    with pytest.raises(sqlite3.ProgrammingError):
        inner.schema_version()
    with pytest.raises(RuntimeError, match="boom"):
        with StateDB(tmp_path / "s.db") as failing:
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        failing.schema_version()


def _ateam_dbs(tracker, since: int) -> list:
    return [e for e in tracker.entries[since:] if e.owner == "ateam"]


def test_cli_short_lived_dbs_and_command_contexts_are_closed(target_repo, _no_leaked_state_db):
    import os
    import sys
    import threading

    import click
    from click.testing import CliRunner

    from ateam import cli
    from ateam.paths import home_dir
    from dbleaks import is_open

    tracker = _no_leaked_state_db
    state = home_dir(target_repo) / "state.db"
    with StateDB(state) as db:
        db.init_schema()

    def opened_and_closed(call) -> None:
        since = len(tracker.entries)
        call()
        made = _ateam_dbs(tracker, since)
        assert made, "the guard opened no StateDB"
        assert not any(is_open(e.db, True) for e in made), [e.site for e in made]

    opened_and_closed(lambda: cli._require_no_live_run_at(target_repo))   # passes: no lock
    with StateDB(state) as db:
        assert db.acquire_run_lock("P-0001")   # held by this live process
    since = len(tracker.entries)
    with pytest.raises(SystemExit) as refused:
        cli._require_no_live_run_at(target_repo)
    assert refused.value.code == 4
    assert _ateam_dbs(tracker, since) and not any(is_open(e.db, True) for e in _ateam_dbs(tracker, since))

    opened_and_closed(lambda: cli._release_own_run_lock(target_repo, "P-0002"))   # not ours: left alone
    with StateDB(state) as db:
        assert db.run_lock_info()["pid"] == os.getpid()
    opened_and_closed(lambda: cli._release_own_run_lock(target_repo, "P-0001"))
    with StateDB(state) as db:
        assert db.run_lock_info() is None

    ctx = cli.Context(target_repo)
    ctx.close()
    ctx.close()
    assert not is_open(ctx.db, True)

    made: list = []

    @click.command()
    @click.option("--code", type=int, default=None)
    def probe(code):
        made.append(cli._context(target_repo))
        assert is_open(made[-1].db, True)
        if code is not None:
            sys.exit(code)

    runner = CliRunner()
    assert runner.invoke(probe, []).exit_code == 0
    assert runner.invoke(probe, ["--code", "3"]).exit_code == 3
    assert len(made) == 2 and not any(is_open(c.db, True) for c in made)
    assert threading.current_thread() is threading.main_thread()


def test_leak_check_reports_unclosed_and_cross_thread_dbs(tmp_path, target_repo, _no_leaked_state_db):
    import threading

    from ateam.cli import Context
    from dbleaks import Tracker, is_open

    by_test = StateDB(tmp_path / "t.db")
    in_test_ctx = Context(target_repo)
    owners = {id(e.db): e.owner for e in _no_leaked_state_db.entries}
    assert owners[id(by_test)] == "test" and owners[id(in_test_ctx.db)] == "test"
    in_test_ctx.close()
    by_test.close()

    tracker, me = Tracker(), threading.get_ident()
    leaked = StateDB(tmp_path / "a.db")
    tracker.track(leaked, "ateam", me, "src/ateam/somewhere.py:12 in opener")
    mine = StateDB(tmp_path / "m.db")
    tracker.track(mine, "test", me, "tests/test_x.py:3 in test_mine")
    made, done, box = threading.Event(), threading.Event(), {}

    def other() -> None:
        db = StateDB(tmp_path / "b.db")
        box["db"] = db
        tracker.track(db, "test", threading.get_ident(), "tests/test_y.py:7 in other")
        made.set()
        done.wait(10)
        db.close()   # only its own thread may

    t = threading.Thread(target=other)
    t.start()
    try:
        assert made.wait(10)
        leaks = tracker.leaks(me)
        assert len(leaks) == 2
        assert any("src/ateam/somewhere.py:12 in opener" in m for m in leaks)
        assert any("tests/test_y.py:7 in other" in m and "another thread" in m for m in leaks)
        tracker.close_test_owned(me)
        assert not is_open(mine, True)
        assert is_open(leaked, True)
        leaked.conn.execute("SELECT 1")   # still usable: the check never closed it
        assert is_open(box["db"], False)
        assert tracker.leaks(me) == leaks
    finally:
        done.set()
        t.join(10)
        leaked.close()
    assert tracker.leaks(me) == []
