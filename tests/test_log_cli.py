"""`ateam log`: one attempt's event log, filtered, followed, and only ever read."""

from __future__ import annotations

from functools import partial

import pytest
from click.testing import CliRunner

from ateam import cli as cli_mod
from ateam.cli import main
from ateam.events import (assistant_text_payload, follow_events as real_follow, format_event, log_path, open_event_log,
                          read_events, session_end_payload, session_start_payload, tool_call_payload)
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir
from ateam.state.db import StateDB


def run(*args):
    return CliRunner().invoke(main, list(args))


@pytest.fixture
def store(target_repo):
    paths = TargetPaths.from_root(target_repo)
    db = StateDB(home_dir(target_repo) / "state.db")
    db.init_schema()
    yield paths, db
    db.close()


def new_attempt(paths, db, contract="C-0001"):
    """An open attempt with its log opened by the real writer; the caller appends and closes the log."""
    aid = db.start_attempt(contract, "build", "light", "m", {})
    row = db.attempt(aid)
    return aid, row, open_event_log(paths, aid, contract, "build", row["started_at"])


def script_follow(monkeypatch, polls):
    """Run `ateam log --follow` with follow_events' sleep replaced by `polls`: one callable per poll, in order.
    The sleep never waits, and a poll beyond the script fails the test instead of hanging it."""
    steps = list(polls)

    def sleep(_seconds):
        if not steps:
            raise AssertionError("follow polled more often than the test expects")
        steps.pop(0)()

    monkeypatch.setattr(cli_mod, "follow_events", partial(real_follow, sleep=sleep))


def test_log_prints_events_in_order(store, target_repo):
    paths, db = store
    aid, row, log = new_attempt(paths, db)
    assert aid == 1
    log.append("session_start", session_start_payload("m", "/w", None, row["started_at"]))
    log.append("assistant_text", assistant_text_payload("hello", 1, None, None))
    log.append("tool_call", tool_call_payload("t1", "Read", {"file_path": "a"}, 1, None))
    log.append("session_end", session_end_payload("success", False, 1, 0.01, "s1"))
    log.close()
    db.finish_attempt(aid, "passed", None, 0, 0, 0.01, 1, 1.0, "s1")
    expected = [format_event(e) for e in read_events(log_path(paths.logs, aid))]

    for ref in ("A-0001", "A-1", "1"):
        r = run("log", ref, "--target", str(target_repo))
        assert r.exit_code == 0, r.output
        assert r.output.splitlines() == expected
    lines = expected
    assert [line.split()[0] for line in lines] == ["A-0001#1", "A-0001#2", "A-0001#3", "A-0001#4"]
    assert [line.split()[2] for line in lines] == ["session_start", "assistant_text", "tool_call", "session_end"]


def test_log_filters_by_type_and_since(store, target_repo):
    paths, db = store
    aid, row, log = new_attempt(paths, db)
    log.append("session_start", session_start_payload("m", "/w", None, row["started_at"]))
    log.append("assistant_text", assistant_text_payload("one", 1, None, None))
    log.append("tool_call", tool_call_payload("t1", "Read", {}, 1, None))
    log.append("assistant_text", assistant_text_payload("two", 2, None, None))
    log.append("session_end", session_end_payload("success", False, 2, None, "s1"))
    log.close()
    db.finish_attempt(aid, "passed", None, 0, 0, None, 2, 1.0, "s1")
    root = str(target_repo)

    r = run("log", "1", "--type", "assistant_text", "--type", "tool_call", "--target", root)
    assert r.exit_code == 0, r.output
    assert [line.split()[0] for line in r.output.splitlines()] == ["A-0001#2", "A-0001#3", "A-0001#4"]

    r = run("log", "1", "--since", "4", "--target", root)
    assert r.exit_code == 0, r.output
    assert [line.split()[0] for line in r.output.splitlines()] == ["A-0001#4", "A-0001#5"]

    r = run("log", "1", "--type", "tool_call", "--since", "4", "--target", root)
    assert r.exit_code == 0 and r.output == ""

    r = run("log", "1", "--type", "nonsense", "--target", root)
    assert r.exit_code == 2 and "Invalid value" in r.output


def test_log_follow_stops_at_session_end_or_closed_attempt(store, target_repo, monkeypatch):
    paths, db = store
    root = str(target_repo)
    aid, row, log = new_attempt(paths, db)
    log.append("session_start", session_start_payload("m", "/w", None, row["started_at"]))
    log.append("assistant_text", assistant_text_payload("start", 1, None, None))

    # Attempt 1 stays open: events arrive during two polls, the second ends with session_end.
    script_follow(monkeypatch, [
        lambda: log.append("tool_call", tool_call_payload("t1", "Read", {}, 1, None)),
        lambda: log.append("session_end", session_end_payload("success", False, 1, None, "s1")),
    ])
    r = run("log", "1", "--follow", "--target", root)
    assert r.exit_code == 0, r.output
    assert [line.split()[0] for line in r.output.splitlines()] == ["A-0001#1", "A-0001#2", "A-0001#3", "A-0001#4"]

    # Attempt 2 is closed in the store while it is quiet: the follow returns without an event.
    aid2, row2, log2 = new_attempt(paths, db)
    log2.append("session_start", session_start_payload("m", "/w", None, row2["started_at"]))
    script_follow(monkeypatch, [lambda: db.finish_attempt(aid2, "killed", "environment", 0, 0, None, 0, 1.0, None)])
    r = run("log", "2", "--follow", "--target", root)
    assert r.exit_code == 0, r.output
    assert [line.split()[0] for line in r.output.splitlines()] == ["A-0002#1"]

    # A finished attempt prints its log and returns at once: no poll is allowed.
    log.close()
    db.finish_attempt(aid, "passed", None, 0, 0, None, 1, 1.0, "s1")
    script_follow(monkeypatch, [])
    r = run("log", "1", "--follow", "--target", root)
    assert r.exit_code == 0, r.output
    assert len(r.output.splitlines()) == 4
    log2.close()


def test_log_follow_reports_a_quiet_orphan_once(store, target_repo, monkeypatch):
    paths, db = store
    aid, row, log = new_attempt(paths, db)
    log.append("session_start", session_start_payload("m", "/w", None, row["started_at"]))
    now = [0.0]
    polls = []

    def sleep(seconds):
        now[0] += 60.0   # each poll is a minute of quiet
        polls.append(seconds)
        if len(polls) == 15:   # 15 quiet minutes, then the attempt is closed in the store, which ends the follow
            db.finish_attempt(aid, "killed", "environment", 0, 0, None, 0, 900.0, None)

    monkeypatch.setattr(cli_mod, "follow_events", partial(real_follow, sleep=sleep, clock=lambda: now[0]))
    r = run("log", "1", "--follow", "--target", str(target_repo))
    assert r.exit_code == 0, r.output
    quiet = [line for line in r.output.splitlines() if "no event for" in line]
    assert len(quiet) == 1, r.output
    assert "A-0001" in quiet[0] and "no event for 10 min" in quiet[0]
    assert "session may be gone" in quiet[0] and "still open in the store" in quiet[0] and "Ctrl-C to stop" in quiet[0]
    log.close()

    help_text = " ".join(run("log", "--help").output.split())   # click wraps the help text
    assert "blocks until Ctrl-C" in help_text


def test_log_unknown_attempt_missing_or_mismatched_log(store, target_repo):
    paths, db = store
    root = str(target_repo)

    r = run("log", "A-0099", "--target", root)
    assert r.exit_code == 4 and "A-0099" in r.output and "Traceback" not in r.output

    r = run("log", "bogus", "--target", root)
    assert r.exit_code == 2

    aid = db.start_attempt("C-0001", "build", "light", "m", {})
    r = run("log", str(aid), "--target", root)
    assert r.exit_code == 1 and "A-0001" in r.output and "no event log" in r.output

    log = open_event_log(paths, aid, "C-0001", "build", "2000-01-01T00:00:00+00:00")
    log.append("session_start", session_start_payload("m", "/w", None, "2000-01-01T00:00:00+00:00"))
    log.close()
    r = run("log", str(aid), "--target", root)
    assert r.exit_code == 1 and "does not match its attempt row" in r.output


def test_log_changes_nothing(store, target_repo):
    paths, db = store
    aid, row, log = new_attempt(paths, db)
    log.append("session_start", session_start_payload("m", "/w", None, row["started_at"]))
    log.append("assistant_text", assistant_text_payload("hi", 1, None, None))
    log.close()
    db.finish_attempt(aid, "passed", None, 0, 0, None, 1, 1.0, "s1")
    git = GitRepo(paths.intent)

    def snapshot():
        return (
            [dict(r) for r in db.conn.execute("SELECT * FROM attempts ORDER BY attempt_id")],
            [dict(r) for r in db.conn.execute("SELECT * FROM pauses ORDER BY pause_id")],
            [dict(r) for r in db.conn.execute("SELECT * FROM transitions ORDER BY seq")],
            git.head(),
            log_path(paths.logs, aid).read_bytes(),
        )

    before = snapshot()
    for args in (["1"], ["A-0001", "--type", "tool_call"], ["1", "--since", "2"], ["1", "--follow"]):
        r = run("log", *args, "--target", str(target_repo))
        assert r.exit_code == 0, r.output
    assert snapshot() == before
