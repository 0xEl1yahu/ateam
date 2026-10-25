"""The policy gate: what a worker may write, what it may run, and record injection on bound paths."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny

from ateam.globs import matches
from ateam.records.model import Record
from ateam.roles import Role

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_OR_WRITE_TOOLS = WRITE_TOOLS | {"Read"}
SCRATCH = ".ateam-scratch/**"

DESTRUCTIVE_PATTERNS: list[str] = [
    r"\bgit\s+push\b",
    r"\bgit\s+merge\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+checkout\s+\.",
    r"\bgit\s+clean\b",
    r"\bgit\s+branch\s+-[dD]\b",
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b",
    r"\bdocker\s+compose\s+down\b",
    r"\bsudo\b",
    r"\bcurl\b.*\|\s*(ba|z)?sh\b",
    r"\bwget\b.*\|\s*(ba|z)?sh\b",
]

INVESTIGATE_ALLOWED_PREFIXES: list[str] = [
    "git log", "git show", "git diff", "git blame", "git status", "git branch", "git rev-parse",
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "tree", "file", "stat", "pwd",
    'python -c "import', "python -m pytest --collect-only", "python --version",
]

_READ_ONLY_PROTECTED = re.compile(r"^\s*(cat|head|tail|less|git\s+diff|git\s+show|git\s+log|ls|wc|grep|rg)\b")
_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n)\s*")


def _protected_mention(cmd: str) -> bool:
    return ".ateam/" in cmd or ".ateam\\" in cmd or ".claude/" in cmd or ".claude\\" in cmd


@dataclass
class Policy:
    worktree: Path
    role: Role
    allowed_paths: list[str]
    protected: list[str]
    bound: dict[str, list[Record]] = field(default_factory=dict)
    denials: list[str] = field(default_factory=list)
    injected: set[str] = field(default_factory=set)

    # -- paths --------------------------------------------------------------

    def relative(self, file_path: str) -> str | None:
        """Repo-relative posix path inside the worktree, or None when it escapes."""
        root = self.worktree.resolve()
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            rel = candidate.resolve().relative_to(root)
        except ValueError:
            return None
        rel_posix = PurePosixPath(rel).as_posix()
        return None if rel_posix == "." else rel_posix

    def path_writable(self, rel: str) -> tuple[bool, str]:
        if any(matches(g, rel) for g in self.protected):
            return False, f"{rel} is protected; contracts and records are read-only for workers"
        if matches(SCRATCH, rel):
            return (self.role != Role.investigate, "INVESTIGATE never writes files")
        if self.role == Role.investigate:
            return False, "INVESTIGATE never writes files"
        if self.role == Role.test:
            return False, "TEST may write only under .ateam-scratch/"
        if any(matches(g, rel) for g in self.allowed_paths):
            return True, ""
        return False, f"{rel} is outside the contract's declared paths"

    # -- commands -----------------------------------------------------------

    def command_allowed(self, cmd: str) -> tuple[bool, str]:
        for pattern in DESTRUCTIVE_PATTERNS:
            if re.search(pattern, cmd):
                return False, f"destructive command matched {pattern!r}"
        segments = [s for s in _SEGMENT_SPLIT.split(cmd) if s.strip()]
        for seg in segments:
            if _protected_mention(seg) and not _READ_ONLY_PROTECTED.match(seg):
                return False, "shell access to .ateam/ or .claude/ is limited to reading"
        if self.role == Role.investigate:
            for seg in segments:
                if not any(seg.lstrip().startswith(p) for p in INVESTIGATE_ALLOWED_PREFIXES):
                    return False, f"INVESTIGATE may only run read-only inspection commands; {seg.strip()[:40]!r} is not allowed"
        return True, ""

    # -- SDK hooks ----------------------------------------------------------

    def _decide(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
        if tool_name in WRITE_TOOLS:
            rel = self.relative(str(tool_input.get("file_path", "")))
            if rel is None:
                return False, "path is outside the worktree"
            return self.path_writable(rel)
        if tool_name == "Bash":
            return self.command_allowed(str(tool_input.get("command", "")))
        return True, ""

    async def can_use_tool(self, tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        ok, reason = self._decide(tool_name, input_data)
        if ok:
            return PermissionResultAllow()
        self.denials.append(f"{tool_name}: {reason}")
        return PermissionResultDeny(message=f"ATeam: {reason}")

    async def pre_tool_use(self, input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input") or {}
        ok, reason = self._decide(tool_name, tool_input)
        if not ok:
            self.denials.append(f"{tool_name}: {reason}")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"ATeam: {reason}",
            }}
        if tool_name in READ_OR_WRITE_TOOLS:
            rel = self.relative(str(tool_input.get("file_path", "")))
            if rel is not None:
                fresh = [r for g, rs in self.bound.items() if matches(g, rel) for r in rs if r.id not in self.injected]
                if fresh:
                    self.injected.update(r.id for r in fresh)
                    text = "\n\n".join(f"ATeam binding record {r.id} ({r.kind}): {r.statement.strip()}" for r in fresh)
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": text,
                    }}
        return {}

    def hook_matchers(self) -> dict[str, list[HookMatcher]]:
        return {"PreToolUse": [HookMatcher(matcher="Edit|Write|MultiEdit|NotebookEdit|Bash|Read", hooks=[self.pre_tool_use])]}
