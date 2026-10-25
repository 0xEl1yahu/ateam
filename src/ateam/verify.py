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
_SCRUB_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_SCRUB_NAMES = {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}


def check_env(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """The environment checks run with: `base` minus credentials and Python path overrides, then `extra` on top.

    Checks execute code a worker wrote, outside the worker's clean room, so they never see the owner's secrets,
    and the worktree's own interpreter is used rather than whichever virtualenv launched ateam."""
    def scrubbed(name: str) -> bool:
        upper = name.upper()
        return upper.startswith(_SCRUB_PREFIXES) or any(w in upper for w in _SCRUB_WORDS) or upper in _SCRUB_NAMES

    env = {k: v for k, v in base.items() if not scrubbed(k)}
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


def _run_one(check: NamedCheck, cwd: Path, env: dict[str, str], timeout: float) -> CheckResult:
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", check.cmd], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except OSError as e:
        return CheckResult(check.name, check.cmd, False, True, None, f"could not start: {e}")
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # the whole group: the shell and everything it started
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        tail = (out.decode(errors="replace") + f"\ntimed out after {timeout:g}s")[-TAIL_CHARS:]
        return CheckResult(check.name, check.cmd, False, True, None, tail)
    tail = out.decode(errors="replace")[-TAIL_CHARS:]
    code = proc.returncode
    return CheckResult(check.name, check.cmd, code == 0, code in ENVIRONMENT_EXITS, code, tail)


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


def acceptance_presence(contract: Contract, root: Path) -> list[AcceptanceResult]:
    """For each `test` criterion `path::name`: the file exists inside the worktree and defines `name` as a word."""
    out: list[AcceptanceResult] = []
    for a in contract.acceptance:
        kind = a.check.type
        if kind != "test":
            out.append(AcceptanceResult(a.id, kind, None, "not checked by minimal verify"))
            continue
        ref = a.check.ref or ""
        file_part, _, name = ref.partition("::")
        path = _inside(root, file_part)
        if not ref or not name:
            out.append(AcceptanceResult(a.id, kind, False, f"test reference {ref!r} does not name path::test"))
        elif path is None:
            out.append(AcceptanceResult(a.id, kind, False, f"{file_part} is outside the worktree"))
        elif not path.is_file():
            out.append(AcceptanceResult(a.id, kind, False, f"acceptance test not found: {file_part} does not exist"))
        elif not re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", path.read_text(errors="replace")):
            out.append(AcceptanceResult(a.id, kind, False, f"acceptance test not found: {name} is not in {file_part}"))
        else:
            out.append(AcceptanceResult(a.id, kind, True, f"{ref} is present"))
    return out


@dataclass(frozen=True)
class Snapshot:
    worktree_head: str
    worktree_dirty: tuple
    intent_head: str
    intent_clean: bool
    owner_head: str
    owner_dirty: tuple


def snapshot(worktree: GitRepo, intent: GitRepo, owner: GitRepo) -> Snapshot:
    return Snapshot(
        worktree.head(), tuple(worktree.status_porcelain()), intent.head(), intent.is_clean(),
        owner.head(), tuple(owner.status_porcelain()),
    )


def tampered(before: Snapshot, after: Snapshot) -> list[str]:
    """Everything the checks changed that they must not: the worktree, the intent repo or the owner's checkout."""
    labels = {
        "worktree_head": "worktree HEAD moved", "worktree_dirty": "worktree files changed",
        "intent_head": "intent repo HEAD moved", "intent_clean": "intent repo changed",
        "owner_head": "owner checkout HEAD moved", "owner_dirty": "owner checkout files changed",
    }
    return [label for field, label in labels.items() if getattr(before, field) != getattr(after, field)]


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
