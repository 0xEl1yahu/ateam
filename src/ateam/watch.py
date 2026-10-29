"""`ateam watch`: tmux argv builders for the viewer. Pure functions plus run_tmux, the only place tmux is executed.
The pane loops live in cli.py; nothing here reads the state store beyond pause_lines' argument."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from ateam.events import attempt_label
from ateam.paths import TargetPaths, repo_id
from ateam.state.db import StateDB

ATTACHING = ("attach-session", "switch-client")   # these need the terminal, so their output is not captured


def session_name(paths: TargetPaths, plan_id: str | None) -> str:
    base = f"ateam-{repo_id(paths.root)}"
    return f"{base}-{plan_id}" if plan_id else base


def run_tmux(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `tmux <argv>`. The attach commands inherit the terminal; every other command's output is captured."""
    if argv and argv[0] in ATTACHING:
        return subprocess.run(["tmux", *argv], text=True, check=False)
    return subprocess.run(["tmux", *argv], text=True, capture_output=True, check=False)


def attach_command(session: str, inside_tmux: bool) -> list[str]:
    return ["switch-client", "-t", session] if inside_tmux else ["attach-session", "-t", session]


def _pane_command(ateam: list[str], target: Path, plan_id: str | None, pane: str) -> str:
    argv = [*ateam, "watch", *([plan_id] if plan_id else []), "--pane", pane, "--target", str(target)]
    return shlex.join(argv)


def attempt_window_command(session: str, ateam: list[str], target: Path, attempt: int) -> list[str]:
    label = attempt_label(attempt)
    follow = shlex.join([*ateam, "log", label, "--follow", "--target", str(target)])
    return ["new-window", "-d", "-t", f"{session}:", "-n", label, follow]


def new_attempt_windows(session: str, ateam: list[str], target: Path, attempts: list[int],
                        existing: set[str]) -> list[list[str]]:
    """Windows to add: one per attempt without a window. Windows of finished attempts are never listed for removal."""
    return [attempt_window_command(session, ateam, target, a) for a in attempts if attempt_label(a) not in existing]


def start_commands(session: str, ateam: list[str], target: Path, plan_id: str | None, attempts: list[int],
                   inside_tmux: bool) -> list[list[str]]:
    commands = [
        ["new-session", "-d", "-s", session, "-n", "status", _pane_command(ateam, target, plan_id, "status")],
        ["new-window", "-d", "-t", f"{session}:", "-n", "pauses", _pane_command(ateam, target, plan_id, "pauses")],
        ["set-option", "-t", session, "remain-on-exit", "on"],
    ]
    commands += new_attempt_windows(session, ateam, target, attempts, set())
    commands.append(attach_command(session, inside_tmux))
    return commands


def pause_lines(db: StateDB, plan_ids: list[str]) -> list[str]:
    lines = []
    for pid in plan_ids:
        pause = db.open_pause(pid)
        if pause is not None:
            lines.append(f"{pid} paused ({pause['reason']}): ateam status {pid}")
    return lines or ["No pause is waiting."]
