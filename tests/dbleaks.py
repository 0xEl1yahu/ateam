"""Test-only leak check for StateDB: which instances a test left open, who made them, and in which thread.

The autouse fixture in conftest.py installs a Tracker by wrapping StateDB.__init__. An instance is owned by 'test' when
the code that called StateDB(...) (or, for the one cli.Context.__init__ makes, the code that called Context(...)) lives
under tests/; otherwise ATeam made it and ATeam must close it."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from ateam.state.db import StateDB

TESTS_DIR = Path(__file__).resolve().parent


@dataclass
class _Entry:
    db: StateDB
    owner: str
    thread_id: int
    site: str


def is_open(db: StateDB, same_thread: bool) -> bool:
    """False once close() ran. In the creating thread a connection closed behind StateDB's back also counts as closed;
    another thread cannot ask sqlite (check_same_thread), so there only close() is seen."""
    if getattr(db, "_closed", False):
        return False
    if not same_thread:
        return True
    try:
        db.conn.total_changes
    except sqlite3.ProgrammingError:
        return False
    return True


def creation_site(frame: FrameType) -> tuple[str, str]:
    """(owner, 'file:line in function') for the frame that called StateDB(...). A StateDB made in cli.Context.__init__
    belongs to whoever called Context(...)."""
    from ateam import cli

    if frame.f_code is cli.Context.__init__.__code__ and frame.f_back is not None:
        frame = frame.f_back
    path = Path(frame.f_code.co_filename).resolve()
    owner = "test" if path.is_relative_to(TESTS_DIR) else "ateam"
    return owner, f"{path}:{frame.f_lineno} in {frame.f_code.co_name}"


class Tracker:
    def __init__(self) -> None:
        self.entries: list[_Entry] = []

    def track(self, db: StateDB, owner: str, thread_id: int, site: str) -> None:
        self.entries.append(_Entry(db, owner, thread_id, site))

    def leaks(self, current_thread_id: int) -> list[str]:
        """Open owner-'ateam' instances, and open instances made in a thread other than current_thread_id."""
        out = []
        for e in self.entries:
            same = e.thread_id == current_thread_id
            if not is_open(e.db, same):
                continue
            if not same:
                out.append(f"StateDB({e.db.path}) made in another thread (owner {e.owner}) is still open; created at {e.site}")
            elif e.owner != "test":
                out.append(f"StateDB({e.db.path}) opened by ATeam was never closed; created at {e.site}")
        return out

    def close_test_owned(self, current_thread_id: int) -> None:
        """Close open owner-'test' instances made in current_thread_id; never touches another thread's connection."""
        for e in self.entries:
            if e.owner == "test" and e.thread_id == current_thread_id and is_open(e.db, True):
                e.db.close()


def install(monkeypatch, tracker: Tracker) -> None:
    """Wrap StateDB.__init__ so every instance is tracked with its creating thread and owner."""
    import threading

    original = StateDB.__init__

    def tracked_init(self, *a, **kw) -> None:
        original(self, *a, **kw)
        owner, site = creation_site(sys._getframe(1))
        tracker.track(self, owner, threading.get_ident(), site)

    monkeypatch.setattr(StateDB, "__init__", tracked_init)
