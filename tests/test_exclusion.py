"""Planning and running exclude each other (owner, 2026-10-26): the helpers that say who holds what."""

import os

from ateam.exclusion import live_planning, process_alive, run_holder
from ateam.state.db import StateDB


def _db(tmp_path):
    db = StateDB(tmp_path / "state.db")
    db.init_schema()
    return db


def _investigating(db, fid, marker=None):
    db.transition("feature", fid, "requested", by="owner", reason="r")
    db.transition("feature", fid, "investigating", by="ateam")
    if marker is not None:
        db.set_meta(f"planning:{fid}", str(marker))


def _dead_pid():
    pid = 4_000_000
    while process_alive(pid):
        pid += 1
    return pid


def test_process_alive():
    assert process_alive(os.getpid()) and not process_alive(_dead_pid())


def test_live_planning_names_a_live_marker_and_ignores_a_dead_one(tmp_path):
    db = _db(tmp_path)
    _investigating(db, "P-0001", os.getpid())
    _investigating(db, "P-0002", _dead_pid())
    _investigating(db, "P-0003")                     # no marker at all
    assert live_planning(db) == [("P-0001", os.getpid())]


def test_live_planning_ignores_a_plan_that_is_not_investigating(tmp_path):
    db = _db(tmp_path)
    _investigating(db, "P-0001", os.getpid())
    db.transition("feature", "P-0001", "plan_proposed", by="ateam")
    assert live_planning(db) == []


def test_run_holder_reports_whether_the_holder_is_alive(tmp_path):
    db = _db(tmp_path)
    assert run_holder(db) is None
    assert db.acquire_run_lock("P-0001")
    assert run_holder(db)["alive"] is True and run_holder(db)["plan_id"] == "P-0001"
    db.release_run_lock("P-0001")
    with db.conn as c:
        c.execute("INSERT INTO run_lock (singleton, plan_id, acquired_at, pid) VALUES (1, 'P-0002', 'then', ?)", (_dead_pid(),))
    held = run_holder(db)
    assert held["plan_id"] == "P-0002" and held["alive"] is False and held["pid"] == _dead_pid()
