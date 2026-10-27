"""Planning and running exclude each other (owner, 2026-10-26): both commit to the intent repo or create refs, and each
side's tamper snapshot would mark the other's sessions `tamper`. These helpers say who holds what; the commands refuse."""

from __future__ import annotations

import os
from typing import Any

from ateam.state.db import StateDB


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_planning(db: StateDB) -> list[tuple[str, int]]:
    """(plan_id, pid) for each investigating feature whose `planning:<id>` marker names a live process."""
    live: list[tuple[str, int]] = []
    for fid in db.features_with_status("investigating"):
        marker = db.get_meta(f"planning:{fid}")
        if marker and marker.isdigit() and process_alive(int(marker)):
            live.append((fid, int(marker)))
    return live


def run_holder(db: StateDB) -> dict[str, Any] | None:
    """The run lock's row (plan_id, pid, acquired_at) plus `alive`; None when no run holds the lock. A row with no
    pid is treated as alive: it is never taken over."""
    info = db.run_lock_info()
    if info is None:
        return None
    pid = info.get("pid")
    return {**info, "alive": pid is None or process_alive(int(pid))}
