"""Minimal verification: the contract's deterministic checks, scope and acceptance presence, judged from the worktree.

This is the bootstrap verifier the owner chose while the full one (checks plus the TEST role) is built through
ATeam itself. It never runs a model and never reads a worker's account of its work. A contract it marks verified
carries `verifier: minimal` in its checkpoint and transition reasons.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ateam.config import NamedCheck
from ateam.contracts.model import Contract
from ateam.gitops import GitRepo
from ateam.treecheck import ScopeResult

VERIFIER = "verifier: minimal"
CHECK_TIMEOUT = 30 * 60
TAIL_CHARS = 4000
ENVIRONMENT_EXITS = (126, 127)   # the shell could not find or execute the command

_SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC")
_SCRUB_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDS")
_SCRUB_SUFFIXES = ("_PWD",)
_SCRUB_NAMES = {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "SSH_AUTH_SOCK", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}


def check_env(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """The environment checks run with: `base` minus credentials and Python path overrides, then `extra` on top.

    Checks execute code a worker wrote, outside the worker's clean room, so they never see the owner's secrets,
    cannot be pointed at another repository, and use the worktree's own interpreter rather than whichever
    virtualenv launched ateam (its bin directory is dropped from PATH)."""
    def scrubbed(name: str) -> bool:
        upper = name.upper()
        return (upper.startswith(_SCRUB_PREFIXES) or any(w in upper for w in _SCRUB_WORDS)
                or upper.endswith(_SCRUB_SUFFIXES) or upper in _SCRUB_NAMES)

    env = {k: v for k, v in base.items() if not scrubbed(k)}
    venv = base.get("VIRTUAL_ENV")
    if venv and "PATH" in env:
        venv_bin = venv.rstrip("/") + "/bin"
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if p.rstrip("/") != venv_bin)
    env.update(extra)
    return env


@dataclass
class CheckResult:
    name: str
    cmd: str
    passed: bool
    errored: bool          # the check could not run at all (missing command, timeout, launch error)
    exit_code: int | None
    tail: str

    @property
    def failure_class(self) -> str | None:
        if self.passed:
            return None
        return "environment" if self.errored else "implementation"

    def fact(self) -> dict:
        return {"name": self.name, "passed": self.passed, "class": self.failure_class, "tail": self.tail}


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass   # the group is already gone


def _run_one(check: NamedCheck, cwd: Path, env: dict[str, str], timeout: float) -> CheckResult:
    """Run one check in its own process group with output to a file, so a background child holding the output
    open can neither hang verify nor fake a timeout. The group is killed when the shell ends (or times out), so
    nothing the check left running can change the tree after it is judged. A child that leaves the group with
    setsid escapes this; that residual is not closed here."""
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", check.cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as e:
            return CheckResult(check.name, check.cmd, False, True, None, f"could not start: {e}")
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill_group(proc.pid)   # also on Ctrl-C: a check never outlives verify
        if timed_out:
            proc.wait()
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - 4 * TAIL_CHARS))
        text = out.read().decode(errors="replace")
    if timed_out:
        return CheckResult(check.name, check.cmd, False, True, None, (text + f"\ntimed out after {timeout:g}s")[-TAIL_CHARS:])
    code = proc.returncode
    return CheckResult(check.name, check.cmd, code == 0, code in ENVIRONMENT_EXITS, code, text[-TAIL_CHARS:])


def run_checks(checks: list[NamedCheck], cwd: Path, env: dict[str, str], timeout: float = CHECK_TIMEOUT) -> list[CheckResult]:
    """Run every check, even after a failure, so the owner sees all of them."""
    return [_run_one(c, cwd, env, timeout) for c in checks]


@dataclass
class AcceptanceResult:
    id: str
    kind: str
    ok: bool | None        # None: not checked by minimal verify (judgment and command criteria)
    detail: str


def _inside(root: Path, rel: str) -> Path | None:
    if not rel or rel.startswith("/"):
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path


def _defines(text: str, name: str) -> bool:
    """`name` is defined at the start of a line (def, class, fn, func, function; async allowed) or named by a
    test(...)/it(...)/describe(...) call. Mentions in comments and other strings do not count."""
    n = re.escape(name)
    return bool(
        re.search(rf"^[ \t]*(?:async[ \t]+)?(?:def|class|fn|func|function)[ \t]+{n}(?![\w])", text, re.M)
        or re.search(rf"^[ \t]*(?:test|it|describe)\(\s*[\"']{n}[\"']", text, re.M)
    )


def acceptance_presence(contract: Contract, root: Path) -> list[AcceptanceResult]:
    """For each `test` criterion `path::name` (or `path::Class::name`): the file exists inside the worktree and
    defines every named part."""
    out: list[AcceptanceResult] = []
    for a in contract.acceptance:
        kind = a.check.type
        if kind != "test":
            out.append(AcceptanceResult(a.id, kind, None, "not checked by minimal verify"))
            continue
        ref = a.check.ref or ""
        file_part, *names = ref.split("::")
        if not file_part or not names or not all(names):
            out.append(AcceptanceResult(a.id, kind, False, f"test reference {ref!r} does not name path::test"))
            continue
        path = _inside(root, file_part)
        if path is None:
            out.append(AcceptanceResult(a.id, kind, False, f"{file_part} is outside the worktree"))
        elif not path.is_file():
            out.append(AcceptanceResult(a.id, kind, False, f"acceptance test not found: {file_part} does not exist"))
        else:
            text = path.read_text(errors="replace")
            missing = [n for n in names if not _defines(text, n)]
            if missing:
                out.append(AcceptanceResult(a.id, kind, False, f"acceptance test not found: {', '.join(missing)} not defined in {file_part}"))
            else:
                out.append(AcceptanceResult(a.id, kind, True, f"{ref} is present"))
    return out


@dataclass(frozen=True)
class Snapshot:
    worktree_head: str
    worktree_tracked: tuple     # porcelain entries for tracked files
    worktree_untracked: tuple   # porcelain entries for untracked files
    intent_head: str
    intent_dirty: tuple
    owner_head: str
    owner_dirty: tuple
    db: str                     # StateDB.fingerprint()


def snapshot(worktree: GitRepo, intent: GitRepo, owner: GitRepo, db_fingerprint: str) -> Snapshot:
    entries = worktree.status_porcelain()
    return Snapshot(
        worktree.head(), tuple(e for e in entries if e[0] != "??"), tuple(e for e in entries if e[0] == "??"),
        intent.head(), tuple(intent.status_porcelain()), owner.head(), tuple(owner.status_porcelain()), db_fingerprint,
    )


_TAMPER = {
    "worktree_head": "worktree HEAD moved", "worktree_tracked": "worktree tracked files changed",
    "intent_head": "intent repo HEAD moved", "intent_dirty": "intent repo files changed",
    "owner_head": "owner checkout HEAD moved", "db": "state store changed",
}


def compare(before: Snapshot, after: Snapshot) -> tuple[list[str], list[str]]:
    """(tamper, warnings) for what changed while the checks ran (owner ruling, 2026-10-25).

    Tamper: the worktree's HEAD or tracked files, the intent repo, the state store, or the owner checkout's HEAD.
    Warnings only: new untracked files in the worktree (build output) and edits in the owner's checkout (the
    owner keeps working while a verify runs)."""
    tamper = [label for field, label in _TAMPER.items() if getattr(before, field) != getattr(after, field)]
    warnings: list[str] = []
    new = sorted(p for _, p in set(after.worktree_untracked) - set(before.worktree_untracked))
    if new:
        warnings.append("new untracked files in the worktree: " + ", ".join(new))
    if before.owner_dirty != after.owner_dirty:
        warnings.append("files changed in your checkout during the checks")
    return tamper, warnings


@dataclass
class Outcome:
    status: str                 # "verified" or "failed"
    failure_class: str | None   # tamper, environment or implementation
    reasons: list[str]


def decide(scope: ScopeResult, checks: list[CheckResult], acceptance: list[AcceptanceResult], tamper: list[str]) -> Outcome:
    """Verified only when nothing was tampered with, the change is in scope, every check passed and every test exists.

    The failure class is the first that applies of tamper, environment, implementation: an environment problem
    means the implementation was never really judged."""
    reasons = list(tamper)
    env = [c for c in checks if c.errored]
    impl = [c for c in checks if not c.passed and not c.errored]
    reasons += [f"{c.name}: errored" for c in env] + [f"{c.name}: failed" for c in impl]
    if not scope.ok:
        reasons.append("scope: outside the contract's paths: " + ", ".join(scope.outside))
    missing = [a for a in acceptance if a.ok is False]
    reasons += [f"{a.id}: {a.detail}" for a in missing]
    if tamper:
        return Outcome("failed", "tamper", reasons)
    if env:
        return Outcome("failed", "environment", reasons)
    if impl or not scope.ok or missing:
        return Outcome("failed", "implementation", reasons)
    return Outcome("verified", None, reasons)


_WALKS = {
    "ready": ["building", "verifying"],
    "building": ["verifying"],
    "failed": ["building", "verifying"],
    "verifying": [],
}


def walk_to(current: str, final: str) -> list[str]:
    """The existing transitions from `current` to `final` (verified or failed); no new edges."""
    return [*_WALKS[current], final]
