"""The policy gate: what a worker may write, what it may run, and record injection on bound paths."""

from __future__ import annotations

import os
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

DESTRUCTIVE_PATTERNS: list[str] = [
    # git <global options>* <verb>; global options cover -C <dir>, -c k=v, --no-pager, etc.
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+(?:push|merge|rebase|clean)\b",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+stash(?!\s+(?:list|show)\b)",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+reset\b[^\n;&|]*--hard",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+(?:checkout|restore)\b[^\n;&|]*\s(?:\.|\./|:/|'\*'|\*)(?:\s|$|[;&|])",
    r"\bgit\b(?:\s+(?:-[cC]\s*\S+|--?\S+))*\s+branch\b[^\n;&|]*\s(?:-[dDMf]|--(?:delete|force|move))\b",
    r"\bdocker\s+compose\s+down\b",
    r"\bsudo\b",
    r"\bcurl\b.*\|\s*(?:ba|z)?sh\b",
    r"\bwget\b.*\|\s*(?:ba|z)?sh\b",
    r"\bfind\b[^\n;&|]*(?:-delete\b|-exec\s+rm\b)",
    r"\bxargs\b[^\n;&|]*\brm\b",
]

INVESTIGATE_ALLOWED_PREFIXES: list[str] = [
    # Read-only inspection only. No interpreter, sed, awk, xargs, env or shell is allow-listed; per-command rules below
    # deny the output flags, -exec forms, pagers and substitutions that could still write or run other programs.
    "git log", "git show", "git diff", "git blame", "git status", "git ls-files", "git grep",
    "git branch --list", "git branch -a", "git branch --show-current", "git rev-parse", "git stash list",
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "du", "stat", "file", "pwd", "sort", "uniq", "cut",
]

# A mention of a protected directory: at the start or after a separator-like character, any case.
_PROTECTED_MENTION = re.compile(r"(?i)(?<![\w-])\.(?:ateam|claude)(?![\w-])")
# Anything that writes, in a segment that mentions a protected directory.
# Commands that change files or git state (no redirects here; redirect targets are checked separately).
_MUTATORS = re.compile(
    r"""(?:\btee\b|\bsed\b[^\n]*\s(?:-[a-zA-Z]*i\b|--in-place\b)|\bcp\b|\bmv\b|\btouch\b|\brm\b|\bmkdir\b|\brmdir\b|\bchmod\b|\bchown\b|\bln\b|\binstall\b|\btruncate\b|\bdd\b|\bpatch\b|\bgit\s+(?:add|commit|checkout|restore|rm|mv|apply)\b)"""
)
# Anything that writes at all, for roles that may not write (INVESTIGATE). `-o` is handled per command below.
_WRITE_OPS = re.compile(r"(?:>>?|<<|\s--output\b|\s-fprintf?\b|\s-fls\b)|" + _MUTATORS.pattern)
_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")
_ENV_PREFIX = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s+\S")
_FIND_EXEC = re.compile(r"^\s*find\b.*\s-(?:exec|execdir|ok|okdir|delete)\b")
_RG_PRE = re.compile(r"^\s*rg\b.*\s--pre(?:-glob)?\b")
_GIT_GREP_PAGER = re.compile(r"^\s*git\s+grep\b.*\s(?:-O\S*|--open-files-in-pager\b)")
_GIT_DASH_C = re.compile(r"^\s*git\s+(?:(?:-C\s*\S+|--no-pager|--git-dir=\S+|--work-tree=\S+)\s+)*-c\b")
_OUTPUT_FLAG_CMDS = re.compile(r"^\s*(?:sort|tree|du)\b.*(?:\s-o\S*|\s--output\b)")
_PAGER = re.compile(r"(?i)core\.pager|\bpager\.|GIT_PAGER|GIT_EXTERNAL_DIFF|GIT_EDITOR|\bLESSOPEN\b")
_GLOBBY_ATEAM = re.compile(r"\.ate[a-z]*[*?\[]")
_REDIRECT_TARGET = re.compile(r">>?\s*(\S+)")
_READ_PREFIX = re.compile(
    r"^\s*(?:cat|head|tail|less|more|wc|grep|rg|ls|stat|file|diff|sort|uniq|cut|awk|find|tree|du|sed\s+-n|git\s+(?:diff|show|log|blame|status|ls-files|grep|branch|stash\s+(?:list|show)))\b"
)
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)(?:^|\n)\2(?:\n|$)", re.S)
_GIT_GLOBAL_OPTS = re.compile(r"^(\s*git)(?:\s+(?:-[cC]\s*\S+|--?\S+))+(\s+)")
_WRAPPERS = {"command", "env", "exec", "nice", "nohup", "time", "builtin"}
_CD = re.compile(r"^\s*(?:cd|pushd)\b")
_GIT_BRANCH = re.compile(r"^\s*git\s+branch\b(?P<rest>.*)$")


def _split_segments(cmd: str) -> list[str]:
    """Split on &&, ||, ;, | and newlines, but never inside single or double quotes."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote and (quote == "'" or cmd[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = cmd[i : i + 2]
        if two in ("&&", "||"):
            segments.append("".join(buf)); buf = []; i += 2; continue
        if ch in (";", "|", "\n"):
            segments.append("".join(buf)); buf = []; i += 1; continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [seg for seg in segments if seg.strip()]


def _strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies so file contents are not mistaken for commands."""
    return _HEREDOC.sub(lambda m: m.group(0)[: m.start(3) - m.start(0)], cmd)


def _normalise_git(segment: str) -> str:
    """`git --no-pager -C x log` -> `git log`, so prefix rules see the verb."""
    return _GIT_GLOBAL_OPTS.sub(r"\1\2", segment, count=1)


def _is_rm(segment: str) -> bool:
    """True for any rm invocation, seen through wrappers, paths and a leading backslash."""
    tokens = segment.split()
    while tokens and tokens[0] in _WRAPPERS:
        tokens = tokens[1:]
    return bool(tokens) and tokens[0].lstrip("\\").rsplit("/", 1)[-1] == "rm"


def _starts_with_prefix(segment: str, prefix: str) -> bool:
    seg = segment.lstrip()
    if not seg.startswith(prefix):
        return False
    if len(seg) == len(prefix) or not prefix[-1].isalnum():
        return True
    return not seg[len(prefix)].isalnum()


def _uniq_writes(segment: str) -> bool:
    tokens = segment.split()
    if not tokens or tokens[0] != "uniq":
        return False
    positional = [t for t in tokens[1:] if not t.startswith("-") or t == "-"]
    return len(positional) >= 2


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
    scratch_dir: Path | None = None   # the worker's own scratch dir under ATeam home; the only writable place outside the worktree
    ateam_home: Path | None = None    # the ATeam home base ($ATEAM_HOME or ~/.ateam); everything in it except scratch_dir is off limits
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

    def _in_scratch(self, file_path: str) -> bool:
        if self.scratch_dir is None:
            return False
        candidate = Path(file_path)
        if not candidate.is_absolute():
            candidate = self.worktree / candidate
        try:
            candidate.resolve().relative_to(self.scratch_dir.resolve())
            return True
        except ValueError:
            return False

    def _spellings(self, path: Path | None) -> list[str]:
        if path is None:
            return []
        out = {str(path), str(path.resolve())}
        return sorted(out, key=len, reverse=True)

    def _under_home_not_scratch(self, file_path: str) -> bool:
        """True when a path resolves into ATeam's home but not into this worker's scratch dir."""
        home = self.ateam_home or Path(os.environ.get("ATEAM_HOME") or (Path.home() / ".ateam"))
        raw = file_path.strip().strip("'\"")
        if raw.startswith("~"):
            raw = os.path.expanduser(raw)
        raw = raw.replace("$HOME", str(Path.home())).replace("${HOME}", str(Path.home()))
        raw = raw.replace("$ATEAM_HOME", str(home)).replace("${ATEAM_HOME}", str(home))
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.worktree / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        try:
            resolved.relative_to(home.resolve())
        except ValueError:
            return False
        return not self._in_scratch(str(resolved))

    def _home_spellings(self) -> list[str]:
        home = self.ateam_home or Path(os.environ.get("ATEAM_HOME") or (Path.home() / ".ateam"))
        out = set(self._spellings(home)) | {"$ATEAM_HOME", "${ATEAM_HOME}", "~/.ateam", "$HOME/.ateam", "${HOME}/.ateam"}
        return sorted(out, key=len, reverse=True)

    def path_writable_abs(self, file_path: str) -> tuple[bool, str]:
        """Writability for any path: inside the worktree by the role rules, inside the worker's scratch dir, nothing else."""
        rel = self.relative(file_path)
        if rel is not None:
            return self.path_writable(rel)
        if self._in_scratch(file_path):
            return (self.role != Role.investigate, "INVESTIGATE never writes files")
        return False, "path is outside the worktree and outside your scratch directory"

    def path_writable(self, rel: str) -> tuple[bool, str]:
        if any(matches(g.lower(), rel.lower()) for g in self.protected):
            return False, f"{rel} is protected; contracts and records are read-only for workers"
        if self.role == Role.investigate:
            return False, "INVESTIGATE never writes files"
        if self.role == Role.test:
            return False, "TEST may write only in its scratch directory (outside the worktree)"
        if any(matches(g, rel) for g in self.allowed_paths):
            return True, ""
        return False, f"{rel} is outside the contract's declared paths"

    # -- commands -----------------------------------------------------------

    def command_allowed(self, cmd: str) -> tuple[bool, str]:
        for pattern in DESTRUCTIVE_PATTERNS:
            if re.search(pattern, cmd):
                return False, f"destructive command matched {pattern!r}"
        segments = _split_segments(_strip_heredocs(cmd))
        scratch_spellings = self._spellings(self.scratch_dir)
        home_spellings = self._home_spellings() if self.scratch_dir is not None or self.ateam_home is not None else []
        for seg in segments:
            mentions_scratch = any(sp in seg for sp in scratch_spellings)
            if mentions_scratch and ".." in seg:
                return False, "paths that leave your scratch directory are not allowed"
            if any(sp in seg for sp in home_spellings) and not mentions_scratch:
                return False, "ATeam's home (intent repo, state store, logs) is off limits to workers; only your scratch directory is writable"
            if home_spellings:
                if _GLOBBY_ATEAM.search(seg.replace('"', "").replace("'", "")):
                    return False, "wildcard paths into ATeam's home are not allowed"
                for tok in seg.replace('"', "").replace("'", "").split():
                    if ("/" in tok or tok.startswith(("~", "$", "."))) and not tok.startswith("-") and self._under_home_not_scratch(tok):
                        return False, "ATeam's home (intent repo, state store, logs) is off limits to workers; only your scratch directory is writable"
            rest = seg
            for sp in [*scratch_spellings, *home_spellings]:
                rest = rest.replace(sp, " ")
            if _is_rm(seg):
                return False, "rm is not allowed for workers; use `git rm` for tracked files, cleanup happens at integration"
            if _PROTECTED_MENTION.search(rest):
                seg = rest
                if _CD.match(seg):
                    return False, "changing directory into .ateam/ or .claude/ is not allowed"
                if _MUTATORS.search(seg):
                    return False, "shell access to .ateam/ or .claude/ is limited to reading"
                if any(_PROTECTED_MENTION.search(t) for t in _REDIRECT_TARGET.findall(seg)):
                    return False, "redirecting output into .ateam/ or .claude/ is not allowed"
                if not _READ_PREFIX.match(_normalise_git(seg)):
                    return False, "shell access to .ateam/ or .claude/ is limited to reading"
        if self.role == Role.investigate:
            if _SUBSTITUTION.search(cmd):
                return False, "INVESTIGATE may not use command or process substitution"
            if _PAGER.search(cmd):
                return False, "INVESTIGATE may not set pagers, editors or diff drivers"
            for seg in segments:
                if _ENV_PREFIX.match(seg):
                    return False, f"INVESTIGATE may not set environment variables for a command; {seg.strip()[:40]!r}"
                if _GIT_DASH_C.match(seg):
                    return False, "INVESTIGATE may not pass git -c"
                norm = _normalise_git(seg)
                if not any(_starts_with_prefix(norm, p) for p in INVESTIGATE_ALLOWED_PREFIXES):
                    return False, f"INVESTIGATE may only run read-only inspection commands; {seg.strip()[:40]!r} is not allowed"
                if (_WRITE_OPS.search(norm) or _FIND_EXEC.match(norm) or _RG_PRE.match(norm) or _GIT_GREP_PAGER.match(norm)
                        or _OUTPUT_FLAG_CMDS.match(norm) or _uniq_writes(norm) or not _git_branch_read_only(norm)):
                    return False, f"INVESTIGATE may not write, run other programs or change state; {seg.strip()[:40]!r} is not allowed"
        return True, ""

    # -- SDK hooks ----------------------------------------------------------

    def _decide(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
        if tool_name in WRITE_TOOLS:
            return self.path_writable_abs(str(tool_input.get("file_path", "")))
        if tool_name == "Bash":
            return self.command_allowed(str(tool_input.get("command", "")))
        if tool_name in ("Read", "Glob", "Grep"):
            target = str(tool_input.get("file_path") or tool_input.get("path") or "")
            if target and self._under_home_not_scratch(target):
                return False, "ATeam's home (intent repo, state store, logs) is not readable by workers; only your scratch directory is"
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
