import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from ateam import events as ev
from ateam.events import (EventLog, TERMINAL_TYPES, PAYLOAD_KEYS, TOOL_RESULT_LIMIT, attempt_label, event_ref,
                          follow_events, log_matches, log_path, open_event_log, parse_attempt, parse_event_ref,
                          prune_logs, read_events, summarise)
from ateam.paths import TargetPaths
from ateam.state.db import StateDB


def mk(tmp_path, name="A-0001.jsonl", attempt=1):
    return EventLog(tmp_path / name, attempt, "C-1", "build", "2026-01-01T00:00:00+00:00")


def err(text="x"):
    return ev.error_payload(text)


def test_append_writes_one_flushed_json_line_per_event(tmp_path):
    lg = mk(tmp_path)
    assert lg.append("error", err("a")) == 1 and lg.append("error", err("b")) == 2
    lines = (tmp_path / "A-0001.jsonl").read_text().splitlines()   # read before close
    assert len(lines) == 2
    d = json.loads(lines[1])
    assert list(d) == ["seq", "at", "attempt", "contract", "role", "type", "payload"]
    assert d["seq"] == 2 and d["attempt"] == 1 and d["contract"] == "C-1" and d["role"] == "build"
    assert datetime.fromisoformat(d["at"]).utcoffset() == timedelta(0) and d["payload"] == {"error": "b"}
    lg.close(); lg.close()


def test_payload_builders_return_exactly_the_keys_of_their_type():
    class Bad:
        def __str__(self):
            return "bad"
    built = {
        "session_start": ev.session_start_payload(None, "/c", None, "t"),
        "assistant_text": ev.assistant_text_payload("hi", 1, None, None),
        "tool_call": ev.tool_call_payload("u", "Bash", {"x": Bad(), "y": {1, 2}}, 2, "m"),
        "tool_result": ev.tool_result_payload("u", "ok", None),
        "policy_denial": ev.policy_denial_payload("Bash", "no"),
        "record_injected": ev.record_injected_payload("Read", "p", ["R-1"]),
        "report": ev.report_payload({"a": Bad()}),
        "session_end": ev.session_end_payload("success", False, 3, None, None),
        "error": ev.error_payload("boom"),
    }
    assert set(built) == set(PAYLOAD_KEYS) == set(ev.EVENT_TYPES)
    for t, p in built.items():
        assert set(p) == PAYLOAD_KEYS[t]
        json.dumps(p)
    assert built["tool_call"]["input"]["x"] == "bad"
    loop = {}; loop["me"] = loop
    assert set(ev.report_payload(loop)) == {"report"}
    assert set(ev.assistant_text_payload(None, "x", 5, 7)) == PAYLOAD_KEYS["assistant_text"]
    assert TERMINAL_TYPES == {"session_end", "error"}

    class NoBool:
        def __bool__(self):
            raise RuntimeError("bool")
    assert set(ev.tool_result_payload("u", "x", NoBool())) == PAYLOAD_KEYS["tool_result"]
    assert set(ev.session_end_payload("s", NoBool(), 1, None, None)) == PAYLOAD_KEYS["session_end"]


def test_append_never_raises_and_counts_failures(tmp_path, caplog):
    lg = mk(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert lg.append("nope", {}) is None
        assert lg.append("error", {"error": "x", "extra": 1}) is None
        assert lg.failures == 2 and len(caplog.records) == 2
    assert lg.append("error", err()) == 1
    lg.close()
    assert lg.append("error", err()) is None and lg.failures == 3
    assert EventLog(None, 1, "C", "r", "t").append("error", err()) is None
    assert len((tmp_path / "A-0001.jsonl").read_text().splitlines()) == 1

    class Boom:
        def write(self, b): raise OSError("disk")
        def flush(self): pass
        def close(self): pass
    lg2 = mk(tmp_path, "B.jsonl"); lg2._fh = Boom()
    assert lg2.append("error", err()) is None and lg2.failures == 1


def test_tool_result_payload_truncates_and_hashes_full_content():
    import hashlib
    big = "é" * (TOOL_RESULT_LIMIT + 10)
    p = ev.tool_result_payload("u", big, True)
    assert len(p["content"]) == TOOL_RESULT_LIMIT and p["truncated"] and p["length"] == len(big)
    assert p["sha256"] == hashlib.sha256(big.encode()).hexdigest()
    lst = [{"b": 1, "a": 2}]
    q = ev.tool_result_payload("u", lst, False)
    ser = json.dumps(lst, sort_keys=True)
    assert q["content"] == ser and not q["truncated"] and q["sha256"] == hashlib.sha256(ser.encode()).hexdigest()


def test_attempt_label_and_event_ref_round_trip():
    assert attempt_label(42) == "A-0042" and attempt_label(12345) == "A-12345"
    assert [parse_attempt(t) for t in ("A-0042", "A-42", "42")] == [42] * 3
    for bad in ("", "a-42", "A42", "-1", "4 2", "A-", "x"):
        with pytest.raises(ValueError):
            parse_attempt(bad)
    assert event_ref(42, 17) == "A-0042#17" and parse_event_ref("A-0042#17") == (42, 17)
    for bad in ("A-0042", "42#17", "A-1#", "A-1#x", "A-1#2#3"):
        with pytest.raises(ValueError):
            parse_event_ref(bad)


def test_open_event_log_moves_a_preexisting_file_aside(target_repo):
    paths = TargetPaths.from_root(target_repo)
    assert not paths.logs.exists()
    first = open_event_log(paths, 1, "C-1", "build", "t1")
    first.append("error", err("old")); first.close()
    second = open_event_log(paths, 1, "C-1", "build", "t2")
    assert second.append("error", err("new")) == 1
    second.close()
    stale = list(paths.logs.glob("A-0001.*.stale.jsonl"))
    assert len(stale) == 1 and "old" in stale[0].read_text()
    assert len(log_path(paths.logs, 1).read_text().splitlines()) == 1
    assert list(target_repo.glob("**/*.jsonl")) == []
    blocked = TargetPaths.from_root(target_repo)
    object.__setattr__(blocked, "scratch_root", log_path(paths.logs, 1) / "x" / "scratch")   # logs under a file
    dead = open_event_log(blocked, 2, "C", "r", "t")
    assert dead.append("error", err()) is None and dead.failures == 1


def _write(path, events):
    with open(path, "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def line(seq, type="error", payload=None, at="2026-01-01T00:00:00+00:00"):
    return {"seq": seq, "at": at, "attempt": 1, "contract": "C", "role": "build", "type": type,
            "payload": payload if payload is not None else err()}


def test_read_events_filters_and_ignores_unterminated_last_line(tmp_path):
    p = tmp_path / "A-0001.jsonl"
    _write(p, [line(1, "report", {"report": {}}), line(2), line(3, "report", {"report": {}})])
    with open(p, "a") as f:
        f.write('{"seq": 4, "at"')
    assert [e.seq for e in read_events(p)] == [1, 2, 3]
    assert [e.seq for e in read_events(p, types={"report"})] == [1, 3]
    assert [e.seq for e in read_events(p, types=["report"], since=2)] == [3]
    assert read_events(p)[0].ref() == "A-0001#1"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(line(1)) + "\nnot json\n")
    with pytest.raises(ValueError, match="line 2"):
        read_events(bad)


def test_follow_events_holds_partial_lines_and_stops_at_terminal_or_closed(tmp_path):
    p = tmp_path / "A-0001.jsonl"
    full = json.dumps(line(1, "report", {"report": {}})) + "\n"
    term = json.dumps(line(2, "session_end", ev.session_end_payload("success", False, 1, None, None))) + "\n"
    steps = [lambda: None,                                   # file does not exist yet
             lambda: p.write_text(full[:10]),
             lambda: open(p, "a").write(full[10:] + term[:5]),
             lambda: open(p, "a").write(term[5:])]
    it = iter(steps)
    got = list(follow_events(p, lambda: True, types={"report"}, sleep=lambda s: next(it)()))
    assert [e.seq for e in got] == [1]   # session_end filtered out but still stops

    q = tmp_path / "B.jsonl"
    q.write_text(json.dumps(line(1)) + "\n" + json.dumps(line(2))[:7])
    state = {"open": True}
    def sleep(_):
        state["open"] = False
    got = list(follow_events(q, lambda: state["open"], types={"error"}, sleep=sleep))
    assert [e.seq for e in got] == [1]   # error is terminal: stops at 1
    r = tmp_path / "C.jsonl"
    r.write_text(json.dumps(line(1, "report", {"report": {}})) + "\n")
    assert [e.seq for e in follow_events(r, lambda: False)] == [1]
    assert list(follow_events(tmp_path / "none.jsonl", lambda: False)) == []


def test_follow_events_reports_a_quiet_open_attempt_once(tmp_path):
    p = tmp_path / "A-0001.jsonl"
    now = [0.0]
    stale = []
    n = [0]
    def sleep(s):
        now[0] += s
        n[0] += 1
        if n[0] == 8:
            p.write_text(json.dumps(line(1, "session_end", ev.session_end_payload("s", False, 0, None, None))) + "\n")
    got = list(follow_events(p, lambda: True, poll_s=10, stale_after_s=25, on_stale=stale.append, sleep=sleep,
                             clock=lambda: now[0]))
    assert len(stale) == 1 and stale[0] >= 25 and [e.seq for e in got] == [1]


def test_summarise_counts_turns_and_latest_event(tmp_path):
    assert summarise([]) == ev.LogSummary(0, 0, None, None)
    p = tmp_path / "x.jsonl"
    _write(p, [line(1, "assistant_text", ev.assistant_text_payload("a", 2, None, None)),
               line(2, "tool_call", ev.tool_call_payload("u", "n", {}, 5, None)),
               line(3, "report", {"report": {}}, at="2026-02-01T10:00:00+00:00")])
    s = summarise(read_events(p))
    assert (s.events, s.turns, s.latest_type) == (3, 5, "report")
    assert s.latest_at == datetime(2026, 2, 1, 10, tzinfo=timezone.utc)


def test_log_matches_only_its_own_attempt_row(tmp_path):
    p = tmp_path / "A-0001.jsonl"
    row = {"attempt_id": 1, "started_at": "S1"}
    assert not log_matches(p, row)
    _write(p, [line(1, "session_start", ev.session_start_payload("m", "/c", None, "S1"))])
    assert log_matches(p, row) and not log_matches(p, {"started_at": "S2"})
    q = tmp_path / "q.jsonl"
    _write(q, [line(1), line(2, "session_start", ev.session_start_payload("m", "/c", None, "S1"))])
    assert not log_matches(q, row)


def test_prune_removes_only_matched_logs_of_attempts_finished_before_retention(tmp_path):
    db = StateDB(tmp_path / "s.db"); db.init_schema()
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)
    ids = [db.start_attempt("C-1", "build", "t", "m", {}) for _ in range(7)]
    old = (now - timedelta(days=20)).isoformat(); recent = (now - timedelta(days=2)).isoformat()
    with db.conn:
        for i, fin in zip(ids, [old, old, old, recent, None, old, old]):
            db.conn.execute("UPDATE attempts SET finished_at=? WHERE attempt_id=?", (fin, i))
    rows = db.telemetry_rows(); db.close()
    logs = tmp_path / "logs"; logs.mkdir()
    for r in rows[:6]:
        if r["attempt_id"] != ids[2]:   # ids[2]: wrong started_at
            _write(log_path(logs, r["attempt_id"]), [line(1, "session_start", ev.session_start_payload("m", "/", None, r["started_at"]))])
    _write(log_path(logs, ids[2]), [line(1, "session_start", ev.session_start_payload("m", "/", None, "other"))])
    stale = logs / "A-0001.20260101T000000000000Z.stale.jsonl"; stale.write_text("x")
    other = logs / "notes.txt"; other.write_text("x")
    orphan = log_path(logs, 99); _write(orphan, [line(1)])
    removed = prune_logs(logs, rows, 14, now, keep={ids[5]})
    assert removed == [log_path(logs, ids[0]), log_path(logs, ids[1])]
    survivors = {f.name for f in logs.iterdir()}
    assert survivors == {log_path(logs, i).name for i in (ids[2], ids[3], ids[4], ids[5], 99)} | {stale.name, "notes.txt"}
