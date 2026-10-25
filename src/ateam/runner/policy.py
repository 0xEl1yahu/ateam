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
    # git <global options>* <verb>; global options cover -C <dir>, -c k=v, --no-pager, etc.
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+(?:push|merge|rebase|clean)\b",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+stash(?!\s+(?:list|show)\b)",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+reset\b[^\n;&|]*--hard",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+(?:checkout|restore)\b[^\n;&|]*\s\.(?:\s|$)",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+branch\b[^\n;&|]*\s-[dDM]\b",
    r"\bdocker\s+compose\s+down\b",
    r"\bsudo\b",
    r"\bcurl\b.*\|\s*(?:ba|z)?sh\b",
    r"\bwget\b.*\|\s*(?:ba|z)?sh\b",
    r"\bfind\b[^\n;&|]*(?:-delete\b|-exec\s+rm\b)",
    r"\bxargs\b[^\n;&|]*\brm\b",
]

INVESTIGATE_ALLOWED_PREFIXES: list[str] = [
    "git log", "git show", "git diff", "git blame", "git status", "git branch", "git rev-parse", "git ls-files",
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "tree", "file", "stat", "pwd", "sort", "uniq", "cut", "awk", "sed -n",
    'python -c "import', "python -m pytest --collect-only", "python --version",
]

_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n|\|)\s*")
# A mention of a protected directory: at the start or after a separator-like character, any case.
_PROTECTED_MENTION = re.compile(r"""(?i)(?:^|[\s/'"=:(`$])\.(?:ateam|claude)(?=$|[\s/'"\\);:`])""")
# Anything that writes, in a segment that mentions a protected directory.
_WRITE_OPS = re.compile(
    r"""(?:>>?|<<|\btee\b|\bsed\b[^\n]*\s-[a-zA-Z]*i\b|\bcp\b|\bmv\b|\btouch\b|\brm\b|\bmkdir\b|\brmdir\b|\bchmod\b|\bchown\b|\bln\b|\binstall\b|\btruncate\b|\bdd\b|\bpatch\b|\bgit\s+(?:add|commit|checkout|restore|rm|mv|apply)\b)"""
)
_READ_PREFIX = re.compile(
    r"^\s*(?:cat|head|tail|less|more|wc|grep|rg|ls|stat|file|diff|sort|uniq|cut|awk|sed\s+-n|git\s+(?:diff|show|log|blame|status|ls-files|grep))\b"
)
_CD = re.compile(r"^\s*(?:cd|pushd)\b")
_GIT_BRANCH = re.compile(r"^\s*git\s+branch\b(?P<rest>.*)$")


def _rm_recursive(segment: str) -> bool:
    tokens = segment.split()
    if not tokens or tokens[0] != "rm":
        return False
    for t in tokens[1:]:
        if t == "--recursive" or (t.startswith("-") and not t.startswith("--") and ("r" in t or "R" in t)):
            return True
    return False


def _starts_with_prefix(segment: str, prefix: str) -> bool:
    seg = segment.lstrip()
    if not seg.startswith(prefix):
        return False
    if len(seg) == len(prefix) or not prefix[-1].isalnum():
        return True
    return not seg[len(prefix)].isalnum()


def _git_branch_read_only(segment: str) -> bool:
    m = _GIT_BRANCH.match(segment)
    if not m:
        return True
    args = m.group("rest").split()
    if any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "-f", "--force", "--delete", "--move", "--copy") for a in args):
        return False
    return all(a.startswith("-") for a in args)


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
        if any(matches(g.lower(), rel.lower()) for g in self.protected):
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
        segments = [seg for seg in _SEGMENT_SPLIT.split(cmd) if seg.strip()]
        for seg in segments:
            if _rm_recursive(seg):
                return False, "recursive rm is not allowed"
            if _PROTECTED_MENTION.search(seg):
                if _CD.match(seg):
                    return False, "changing directory into .ateam/ or .claude/ is not allowed"
                if _WRITE_OPS.search(seg) or not _READ_PREFIX.match(seg):
                    return False, "shell access to .ateam/ or .claude/ is limited to reading"
        if self.role == Role.investigate:
            for seg in segments:
                if not any(_starts_with_prefix(seg, p) for p in INVESTIGATE_ALLOWED_PREFIXES):
                    return False, f"INVESTIGATE may only run read-only inspection commands; {seg.strip()[:40]!r} is not allowed"
                if _WRITE_OPS.search(seg) or not _git_branch_read_only(seg):
                    return False, f"INVESTIGATE may not write or change state; {seg.strip()[:40]!r} is not allowed"
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
