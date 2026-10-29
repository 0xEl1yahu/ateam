"""Per-attempt event log: one JSON object per line in <home>/logs/A-NNNN.jsonl.

The writer never raises (a logging failure must not fail an attempt); the reader and follower tolerate a
line cut by a crash. Payload shapes are fixed per type in PAYLOAD_KEYS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ateam.paths import TargetPaths

log = logging.getLogger(__name__)

EVENT_TYPES: tuple[str, ...] = ("session_start", "assistant_text", "tool_call", "tool_result", "policy_denial",
                                "record_injected", "report", "session_end", "error")
TERMINAL_TYPES: frozenset[str] = frozenset({"session_end", "error"})
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "session_start": frozenset({"model", "cwd", "resume", "started_at"}),
    "assistant_text": frozenset({"text", "turn", "message_id", "sdk_error"}),
    "tool_call": frozenset({"tool_use_id", "name", "input", "turn", "message_id"}),
    "tool_result": frozenset({"tool_use_id", "is_error", "content", "truncated", "length", "sha256"}),
    "policy_denial": frozenset({"tool", "reason"}),
    "record_injected": frozenset({"tool", "path", "records"}),
    "report": frozenset({"report"}),
    "session_end": frozenset({"subtype", "is_error", "num_turns", "cost_usd", "session_id"}),
    "error": frozenset({"error"}),
}
TOOL_RESULT_LIMIT = 4000


# -- payload builders (never raise) ----------------------------------------------------------------

def _safe(value: Any) -> Any:
    """A JSON-serialisable copy of value; whatever cannot be serialised becomes its str()."""
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        try:
            return str(value)
        except Exception:
            return "<unprintable>"


def _str(value: Any) -> str:
    try:
        return value if isinstance(value, str) else str(value)
    except Exception:
        return "<unprintable>"


def _opt_str(value: Any) -> str | None:
    return None if value is None else _str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def session_start_payload(model: str | None, cwd: str, resume: str | None, started_at: str) -> dict[str, Any]:
    return {"model": _opt_str(model), "cwd": _str(cwd), "resume": _opt_str(resume), "started_at": _str(started_at)}


def assistant_text_payload(text: str, turn: int, message_id: str | None, sdk_error: str | None) -> dict[str, Any]:
    return {"text": _str(text), "turn": _int(turn), "message_id": _opt_str(message_id), "sdk_error": _opt_str(sdk_error)}


def tool_call_payload(tool_use_id: str, name: str, input: dict[str, Any], turn: int, message_id: str | None) -> dict[str, Any]:
    return {"tool_use_id": _str(tool_use_id), "name": _str(name), "input": _safe(input), "turn": _int(turn),
            "message_id": _opt_str(message_id)}


def tool_result_payload(tool_use_id: str, content: Any, is_error: bool | None) -> dict[str, Any]:
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    elif isinstance(content, (list, dict)):
        try:
            text = json.dumps(content, sort_keys=True, default=str)
        except Exception:
            text = _str(content)
    else:
        text = _str(content)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return {"tool_use_id": _str(tool_use_id), "is_error": None if is_error is None else bool(is_error),
            "content": text[:TOOL_RESULT_LIMIT], "truncated": len(text) > TOOL_RESULT_LIMIT,
            "length": len(text), "sha256": digest}


def policy_denial_payload(tool: str, reason: str) -> dict[str, Any]:
    return {"tool": _str(tool), "reason": _str(reason)}


def record_injected_payload(tool: str, path: str, records: list[str]) -> dict[str, Any]:
    try:
        ids = [_str(r) for r in records]
    except Exception:
        ids = [_str(records)]
    return {"tool": _str(tool), "path": _str(path), "records": ids}


def report_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {"report": _safe(report)}


def session_end_payload(subtype: str, is_error: bool, num_turns: int, cost_usd: float | None, session_id: str | None) -> dict[str, Any]:
    try:
        cost = None if cost_usd is None else float(cost_usd)
    except Exception:
        cost = None
    return {"subtype": _str(subtype), "is_error": bool(is_error), "num_turns": _int(num_turns), "cost_usd": cost,
            "session_id": _opt_str(session_id)}


def error_payload(error: str) -> dict[str, Any]:
    return {"error": _str(error)}


# -- event model -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    seq: int
    at: str
    attempt: int
    contract: str
    role: str
    type: str
    payload: dict[str, Any]

    def ref(self) -> str:
        return event_ref(self.attempt, self.seq)


def attempt_label(attempt: int) -> str:
    return f"A-{attempt:04d}"


def parse_attempt(text: str) -> int:
    m = re.fullmatch(r"(?:A-)?([0-9]+)", text, re.ASCII) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"not an attempt id: {text!r}")
    return int(m.group(1))


def event_ref(attempt: int, seq: int) -> str:
    return f"{attempt_label(attempt)}#{seq}"


def parse_event_ref(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"A-([0-9]+)#([0-9]+)", ref, re.ASCII) if isinstance(ref, str) else None
    if not m:
        raise ValueError(f"not an event ref: {ref!r}")
    return int(m.group(1)), int(m.group(2))


def log_path(logs_dir: Path, attempt: int) -> Path:
    return logs_dir / f"{attempt_label(attempt)}.jsonl"


# -- writer ----------------------------------------------------------------------------------------

class EventLog:
    """Single writer for one attempt's log. append() never raises; failures are counted, not propagated."""

    def __init__(self, path: Path | None, attempt: int, contract: str, role: str, started_at: str) -> None:
        self.path = path
        self.attempt = attempt
        self.contract = contract
        self.role = role
        self.started_at = started_at
        self.failures = 0
        self._seq = 0
        self._lock = threading.Lock()
        self._fh: Any = None
        self._closed = False
        if path is not None:
            try:
                self._fh = open(path, "ab", buffering=0)
            except Exception as exc:
                log.warning("event log %s cannot be opened: %s", path, exc)

    def append(self, type: str, payload: dict[str, Any]) -> int | None:
        with self._lock:
            try:
                if self._fh is None or self._closed:
                    raise OSError("event log is not open")
                keys = PAYLOAD_KEYS.get(type)
                if keys is None:
                    raise ValueError(f"unknown event type {type!r}")
                if not isinstance(payload, dict) or set(payload) != keys:
                    raise ValueError(f"payload keys of {type!r} differ from the schema")
                seq = self._seq + 1
                line = json.dumps({"seq": seq, "at": datetime.now(timezone.utc).isoformat(), "attempt": self.attempt,
                                   "contract": self.contract, "role": self.role, "type": type, "payload": payload},
                                  default=str) + "\n"
                self._fh.write(line.encode("utf-8"))
                self._fh.flush()
                self._seq = seq
                return seq
            except Exception as exc:
                self.failures += 1
                try:
                    log.warning("event %s of attempt %s not written: %s", type, self.attempt, exc)
                except Exception:
                    pass
                return None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            fh, self._fh = self._fh, None
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass


def open_event_log(paths: TargetPaths, attempt: int, contract: str, role: str, started_at: str) -> EventLog:
    """A fresh log for the attempt. A pre-existing file is moved aside, never appended to. Never raises."""
    path: Path | None = None
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        path = log_path(paths.logs, attempt)
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            os.replace(path, path.with_name(f"{attempt_label(attempt)}.{stamp}.stale.jsonl"))
    except Exception as exc:
        log.warning("event log of attempt %s cannot be prepared: %s", attempt, exc)
        path = None
    return EventLog(path, attempt, contract, role, started_at)


# -- reader ----------------------------------------------------------------------------------------

def _parse_line(raw: bytes, lineno: int) -> Event:
    try:
        d = json.loads(raw.decode("utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not an object")
        return Event(seq=int(d["seq"]), at=str(d["at"]), attempt=int(d["attempt"]), contract=str(d["contract"]),
                     role=str(d["role"]), type=str(d["type"]), payload=dict(d["payload"]))
    except Exception as exc:
        raise ValueError(f"malformed event at line {lineno}: {exc}") from exc


def _wanted(ev: Event, types: Collection[str] | None, since: int | None) -> bool:
    return (types is None or ev.type in types) and (since is None or ev.seq >= since)


def read_events(path: Path, types: Collection[str] | None = None, since: int | None = None) -> list[Event]:
    data = Path(path).read_bytes()
    lines = data.split(b"\n")[:-1]   # the last piece is empty or an unterminated line
    events = (_parse_line(raw, n) for n, raw in enumerate(lines, 1))
    return [ev for ev in events if _wanted(ev, types, since)]


def follow_events(path: Path, is_open: Callable[[], bool], types: Collection[str] | None = None,
                  since: int | None = None, poll_s: float = 0.5, stale_after_s: float = 600.0,
                  on_stale: Callable[[float], None] | None = None, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> Iterator[Event]:
    """Tail a log. Stops after a terminal event, or when the attempt is closed and no complete line remains."""
    pos, buf, lineno = 0, b"", 0
    last_progress, notified = clock(), False
    while True:
        open_now = is_open()
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
            pos += len(chunk)
            buf += chunk
        except FileNotFoundError:
            pass
        progressed = False
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            lineno += 1
            progressed = True
            ev = _parse_line(raw, lineno)
            if _wanted(ev, types, since):
                yield ev
            if ev.type in TERMINAL_TYPES:
                return
        if progressed:
            last_progress, notified = clock(), False
            continue
        if not open_now:
            return
        quiet = clock() - last_progress
        if on_stale is not None and not notified and quiet >= stale_after_s:
            notified = True
            on_stale(quiet)
        sleep(poll_s)


def format_event(event: Event) -> str:
    p = event.payload
    def clip(s: Any, n: int = 80) -> str:
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[: n - 1] + "…"
    t = event.type
    if t == "session_start":
        detail = f"model={p.get('model')} cwd={p.get('cwd')}" + (f" resume={p['resume']}" if p.get("resume") else "")
    elif t == "assistant_text":
        detail = clip(p.get("text", "")) + (f" [sdk_error={p['sdk_error']}]" if p.get("sdk_error") else "")
    elif t == "tool_call":
        detail = f"{p.get('name')} {clip(json.dumps(p.get('input'), default=str, sort_keys=True))}"
    elif t == "tool_result":
        detail = f"{'error ' if p.get('is_error') else ''}{p.get('length')} chars " + clip(p.get("content", ""), 60)
    elif t == "policy_denial":
        detail = f"{p.get('tool')}: {clip(p.get('reason', ''))}"
    elif t == "record_injected":
        detail = f"{p.get('tool')} {p.get('path')}: {', '.join(p.get('records') or [])}"
    elif t == "report":
        detail = clip(json.dumps(p.get("report"), default=str, sort_keys=True))
    elif t == "session_end":
        detail = f"{p.get('subtype')} turns={p.get('num_turns')} cost={p.get('cost_usd')}" + (" error" if p.get("is_error") else "")
    elif t == "error":
        detail = clip(p.get("error", ""))
    else:
        detail = ""
    return f"{event.ref()} {event.at} {t} {detail}".rstrip()


# -- summary, matching, pruning --------------------------------------------------------------------

@dataclass
class LogSummary:
    events: int
    turns: int
    latest_type: str | None
    latest_at: datetime | None


def _parse_time(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summarise(events: list[Event]) -> LogSummary:
    turns = [int(e.payload.get("turn") or 0) for e in events if e.type in ("assistant_text", "tool_call")]
    last = events[-1] if events else None
    return LogSummary(events=len(events), turns=max(turns, default=0),
                      latest_type=last.type if last else None, latest_at=_parse_time(last.at) if last else None)


def log_matches(path: Path, attempt_row: dict[str, Any]) -> bool:
    """True only when the log's first event is a session_start carrying the row's started_at."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline()
        if not first.endswith(b"\n"):
            return False
        ev = _parse_line(first, 1)
    except (OSError, ValueError):
        return False
    return ev.type == "session_start" and ev.payload.get("started_at") == attempt_row.get("started_at")


def prune_logs(logs_dir: Path, attempts: list[dict[str, Any]], retention_days: int, now: datetime,
               keep: Collection[int] = ()) -> list[Path]:
    """Remove logs of attempts finished more than retention_days before now. Only A-NNNN.jsonl files that match their row."""
    rows = {r["attempt_id"]: r for r in attempts}
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    removed: list[Path] = []
    if not Path(logs_dir).is_dir():
        return removed
    for path in sorted(Path(logs_dir).iterdir()):
        m = re.fullmatch(r"A-([0-9]+)\.jsonl", path.name, re.ASCII)
        if not m or not path.is_file():
            continue
        attempt = int(m.group(1))
        row = rows.get(attempt)
        if row is None or attempt in keep or not row.get("finished_at"):
            continue
        finished = _parse_time(row["finished_at"])
        if finished is None or finished >= cutoff or not log_matches(path, row):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed
