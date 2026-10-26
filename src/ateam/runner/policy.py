"""The policy gate: what a worker may write, what it may run, and record injection on bound paths."""

from __future__ import annotations

import itertools
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
_WRITE_OPS = re.compile(r"(?:>>?|<<|\s--output\b|\s-fprint(?:0|f)?\b|\s-fls\b)|" + _MUTATORS.pattern)
_ENV_PREFIX = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s+\S")
_FIND_EXEC = re.compile(r"^\s*find\b.*\s-(?:exec|execdir|ok|okdir|delete)\b")
_RG_PRE = re.compile(r"^\s*rg\b.*\s--pre(?:-glob)?\b")
_GIT_GREP_PAGER = re.compile(r"^\s*git\s+grep\b.*\s(?:-O\S*|--open-files-in-pager\b)")
_GIT_DASH_C = re.compile(r"^\s*git\s+(?:(?:-C\s*\S+|--no-pager|--git-dir=\S+|--work-tree=\S+)\s+)*-c\b")
_OUTPUT_FLAG_CMDS = re.compile(r"^\s*(?:tree|du)\b.*(?:\s-o\S*|\s--output\b)|^\s*sort\b.*(?:\s-[A-Za-z]*o(?:\S*)?(?:\s|$)|\s--output\b)")
_PAGER = re.compile(r"(?i)core\.pager|\bpager\.|GIT_PAGER|GIT_EXTERNAL_DIFF|GIT_EDITOR|\bLESSOPEN\b")
_WILDCARD = re.compile(r"[*?\[{]")
_REDIRECT_TARGET = re.compile(r">>?\s*(\S+)")
_READ_PREFIX = re.compile(
    r"^\s*(?:cat|head|tail|less|more|wc|grep|rg|ls|stat|file|diff|sort|uniq|cut|awk|find|tree|du|sed\s+-n|git\s+(?:diff|show|log|blame|status|ls-files|grep|branch|stash\s+(?:list|show)))\b"
)
_GIT_GLOBAL_OPTS = re.compile(r"^(\s*git)(?:\s+(?:-[cC]\s*\S+|--?\S+))+(\s+)")
_WRAPPERS = {"command", "env", "exec", "nice", "nohup", "time", "builtin", "timeout", "stdbuf"}
# Wrapper options whose argument is the next word (`nice -n 5 rm`), and positional arguments before the command.
_WRAPPER_ARG_OPTS = {
    "nice": {"-n", "--adjustment"},
    "env": {"-u", "--unset", "-C", "--chdir", "-P"},
    "time": {"-f", "--format", "-o", "--output"},
    "exec": {"-a"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
}
_WRAPPER_POSITIONAL = {"timeout": 1}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh"}
_NESTING_LIMIT = 8   # eval and shell -c bodies are re-lexed this deep; deeper is denied
_READ_TOOL_HINT_WORDS = {"sed", "awk", "python", "python3", "perl", "ruby", "node"}
_CD = re.compile(r"^\s*(?:cd|pushd)\b")
CD_WORKTREE_REASON = "workers may cd only to an absolute path inside the worktree"  # every role; see Policy._cd_target
NESTING_REASON = "eval or shell -c nested too deeply to check"
_GIT_BRANCH = re.compile(r"^\s*git\s+branch\b(?P<rest>.*)$")


# ---- the lexer ---------------------------------------------------------------------------------------------------
# One walk over the command that follows bash's grammar for where commands begin and end (bash manual: Quoting,
# Comments, Here Documents, Lists, Pipelines, Compound Commands, Command Substitution, Arithmetic Expansion).
# Line continuations are removed as the walk reads characters, exactly where bash removes them: everywhere except
# inside '...', $'...', comments and the bodies of here-documents whose delimiter is quoted.

_RESERVED_LEADING = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time", "coproc"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_FD_WORD = re.compile(r"^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})$")
_ANSI_C_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                  "\\": "\\", "'": "'", '"': '"', "?": "?"}
_LIST_OPS = (";;&", ";;", ";&", ";", "&&", "&", "||", "|&", "|")
_REDIRECT_OPS = (">>", ">|", ">&", "<&", "<>", ">", "<")


def _ansi_c(body: str) -> str:
    """The value of the text between $' and ' (the escapes that matter for reading a command word)."""
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out.append(c); i += 1
            continue
        e = body[i + 1]
        m = None
        if e in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[e]); i += 2
        elif e == "x" and (m := re.match(r"[0-9A-Fa-f]{1,2}", body[i + 2 :])):
            out.append(chr(int(m.group(), 16))); i += 2 + len(m.group())
        elif e in "uU" and (m := re.match(r"[0-9A-Fa-f]{1,%d}" % (4 if e == "u" else 8), body[i + 2 :])):
            out.append(chr(min(int(m.group(), 16), 0x10FFFF))); i += 2 + len(m.group())
        elif e in "01234567" and (m := re.match(r"[0-7]{1,3}", body[i + 1 :])):
            out.append(chr(int(m.group(), 8) & 0xFF)); i += 1 + len(m.group())
        elif e == "c" and i + 2 < len(body):
            out.append(chr(ord(body[i + 2]) & 0x1F)); i += 3
        else:
            out.append(c + e); i += 2
    return "".join(out)


@dataclass
class _Segment:
    """One simple command: its text (continuations, comments and heredoc bodies removed) and its words after
    quote removal, without redirections and here-document delimiters."""
    text: str
    words: list[str]


@dataclass
class _Lexed:
    joined: str               # the command with bash's line continuations removed; comments and heredoc bodies kept
    stripped: str             # joined without heredoc bodies
    masked: str               # joined with every '...' and $'...' replaced by ''
    segments: list[_Segment]  # simple commands in order, including those inside substitutions
    substitution: bool        # $( ), $(( )), ` `, <( ) or >( ) that bash would expand (not in '...', comments or quoted bodies)


class _Lexer:
    def __init__(self, s: str) -> None:
        self.s, self.n = s, len(s)
        self.removed: set[int] = set()                  # index of the backslash of each removed backslash-newline
        self.comments: list[tuple[int, int]] = []
        self.bodies: list[tuple[int, int]] = []         # heredoc body plus terminator line
        self.single: list[tuple[int, int]] = []         # '...' and $'...' including the quotes
        self.items: list[tuple[int, int, list[str]] | _Segment] = []
        self.substitution = False
        self.newlines = 0                               # newline tokens read at any depth

    # -- characters ---------------------------------------------------------------------------------------------

    def _skip(self, i: int) -> int:
        """The next index at or after i once line continuations are removed (bash removes them as it reads)."""
        while self.s.startswith("\\\n", i):
            self.removed.add(i)
            i += 2
        return i

    def _match(self, i: int, text: str) -> int | None:
        """End index if the characters from i spell text, line continuations between them removed."""
        for ch in text:
            i = self._skip(i)
            if self.s[i : i + 1] != ch:
                return None
            i += 1
        return i

    # -- quoting and expansions ---------------------------------------------------------------------------------

    def _ansi_end(self, i: int) -> int:
        """Index after the ' that closes a $'...' whose text starts at i (backslash escapes the next character)."""
        while i < self.n:
            if self.s[i] == "\\":
                i += 2
            elif self.s[i] == "'":
                return i + 1
            else:
                i += 1
        return self.n

    def _dquote(self, i: int, val: list[str], expand: bool = True) -> int:
        """Read the inside of "..." from i; return the index after the closing quote."""
        s, n = self.s, self.n
        while i < n:
            i = self._skip(i)
            if i >= n:
                break
            c = s[i]
            if c == "\\":
                nxt = s[i + 1 : i + 2]
                if nxt in ("$", "`", '"', "\\"):
                    val.append(nxt); i += 2
                else:
                    val.append(c); i += 1
                continue
            if c == '"':
                return i + 1
            if expand and c == "`":
                j = self._backtick(i + 1); val.append(s[i:j]); i = j
                continue
            if expand and c == "$":
                j = self._dollar(i, in_dquote=True)
                if j is not None:
                    val.append(s[i:j]); i = j
                    continue
            val.append(c); i += 1
        return n

    def _dollar(self, i: int, in_dquote: bool = False) -> int | None:
        """End index of the $-expansion at i that can hold commands ($( ), $(( )), ${ }, ${ cmd; }, $[ ]), else None."""
        j = self._match(i, "$((")
        if j is not None:
            end = self._arith(j, "))")
            if end is not None:
                self.substitution = True   # kept as before: INVESTIGATE never had $(( )); allowing it is the owner's call
                return end
        j = self._match(i, "$(")
        if j is not None:
            self.substitution = True
            return self._list(j, stop=")")
        j = self._match(i, "${")
        if j is not None:
            k = self._skip(j)
            if self.s[k : k + 1] in (" ", "\t", "\n", "|"):   # bash 5.3 ${ cmd; } and ${| cmd; } run a command list
                self.substitution = True
                return self._list(k + 1 if self.s[k] == "|" else k, stop="}")
            return self._braced(j, in_dquote)
        j = self._match(i, "$[")
        if j is not None:
            end = self._arith(j, "]")
            return self.n if end is None else end
        return None

    def _arith(self, i: int, close: str) -> int | None:
        """Index after the close of an arithmetic expression starting at i; None when it is not one (bash then
        reads $(( as a command substitution holding a subshell). Inside, << is a shift, never a heredoc."""
        s, n = self.s, self.n
        depth = 0
        opener = "(" if close == "))" else "["
        while i < n:
            i = self._skip(i)
            if i >= n:
                break
            c = s[i]
            if c == "\\":
                i += 2
            elif c == '"':
                i = self._dquote(i + 1, [])
            elif c == "`":
                i = self._backtick(i + 1)
            elif c == "$" and (j := self._dollar(i)) is not None:
                i = j
            elif c == opener:
                depth += 1; i += 1
            elif c == close[0]:
                if depth:
                    depth -= 1; i += 1
                elif close == "]":
                    return i + 1
                else:
                    return self._match(i, "))")
            else:
                i += 1
        return None

    def _braced(self, i: int, in_dquote: bool) -> int:
        """Index after the } that closes a ${ whose text starts at i."""
        s, n = self.s, self.n
        while i < n:
            i = self._skip(i)
            if i >= n:
                break
            c = s[i]
            if c == "\\":
                i += 2
            elif c == "}":
                return i + 1
            elif c == "'" and not in_dquote:
                j = s.find("'", i + 1)
                i = n if j < 0 else j + 1
            elif c == '"':
                i = self._dquote(i + 1, [])
            elif c == "`":
                i = self._backtick(i + 1)
            elif c == "$" and (j := self._dollar(i, in_dquote)) is not None:
                i = j
            else:
                i += 1
        return n

    def _backtick(self, i: int) -> int:
        """Index after the closing backtick of `...` whose text starts at i. The text, with \\$ \\` \\\\ unescaped,
        is lexed as a command list of its own."""
        s, n = self.s, self.n
        buf: list[str] = []
        while i < n:
            i = self._skip(i)
            if i >= n:
                break
            c = s[i]
            if c == "\\" and i + 1 < n:
                nxt = s[i + 1]
                buf.append(nxt if nxt in "$`\\" else c + nxt)
                i += 2
                continue
            if c == "`":
                break
            buf.append(c); i += 1
        self.substitution = True
        self.newlines += any("\n" in b for b in buf)   # a heredoc pending outside does not start its body in here
        inner = _Lexer("".join(buf))
        inner._list(0)
        self.items.extend(inner._segments())
        return min(i + 1, n)

    def _delimiter(self, i: int) -> tuple[int, str, bool]:
        """Read a here-document delimiter word from i: (end, terminator after quote removal, quoted?).
        The word ends at a space, tab, newline or one of ; & | < > ( )."""
        s, n = self.s, self.n
        val: list[str] = []
        quoted = False
        while True:
            i = self._skip(i)
            if i >= n or s[i] in " \t\n;&|<>()":
                return i, "".join(val), quoted
            c = s[i]
            if c == "\\":
                quoted = True; val.append(s[i + 1 : i + 2]); i += 2
            elif c == "'":
                quoted = True
                j = s.find("'", i + 1)
                j = n if j < 0 else j
                val.append(s[i + 1 : j]); i = j + 1
            elif c == '"':
                quoted = True; i = self._dquote(i + 1, val, expand=False)
            elif (j := self._match(i, "$'")) is not None:
                quoted = True
                end = self._ansi_end(j)
                val.append(_ansi_c(s[j : end - 1] if end <= n and s[end - 1 : end] == "'" else s[j:end])); i = end
            elif (j := self._match(i, '$"')) is not None:
                quoted = True; i = self._dquote(j, val, expand=False)
            else:
                val.append(c); i += 1

    def _bodies(self, i: int, pending: list[tuple[str, bool, bool]]) -> int:
        """Consume the here-document bodies that start at i, in order. Returns the index after the last terminator
        line, or i unchanged when any body is unterminated (its lines then stay command text: fail closed)."""
        s, n = self.s, self.n
        found = []
        pos = i
        for term, dash, quoted in pending:
            start = pos
            while True:
                if pos >= n:
                    return i
                k, line, conts = pos, [], []
                while k < n and s[k] != "\n":
                    if not quoted and s[k] == "\\" and k + 1 < n:
                        if s[k + 1] == "\n":
                            conts.append(k); k += 2
                            continue
                        line.append(s[k : k + 2]); k += 2
                        continue
                    line.append(s[k]); k += 1
                text = "".join(line)
                nxt = min(k + 1, n)
                if (text.lstrip("\t") if dash else text) == term:
                    found.append((start, pos, nxt, quoted, conts))
                    pos = nxt
                    break
                found.append((None, None, None, quoted, conts))
                pos = nxt
        for start, body_end, end, quoted, conts in found:
            if not quoted:
                self.removed.update(conts)
            if start is not None:
                self.bodies.append((start, end))
                if not quoted:
                    self._body_expansions(start, body_end)
        return pos

    def _body_expansions(self, i: int, end: int) -> None:
        """An unquoted here-document body undergoes command substitution: lex every $( ) and ` ` in it."""
        s = self.s
        while i < end:
            c = s[i]
            if c == "\\":
                i += 2
            elif c == "`":
                i = self._backtick(i + 1)
            elif c == "$" and (j := self._dollar(i)) is not None:
                i = j
            else:
                i += 1

    def _closes_word(self, i: int) -> bool:
        """True when the character at i (continuations skipped) ends a word: the end, a blank or an operator."""
        i = self._skip(i)
        return i >= self.n or self.s[i] in " \t\n;&|<>()"

    # -- command lists ------------------------------------------------------------------------------------------

    def _list(self, i: int, stop: str = "") -> int:
        """Lex a command list from i to the end, or to the stop that closes it: the ) of a $( or <( (stop=")"), or
        the } of a ${ cmd; } (stop="}"). Returns the index after."""
        s, n = self.s, self.n
        seg_start = i
        words: list[str] = []
        val: list[str] = []
        started = False      # a word is being read (any character, quote or expansion seen)
        target = False       # the next word is a redirection target, not a command word
        depth = 0            # open ( subshells inside this list
        pending: list[tuple[str, bool, bool, int]] = []
        cases: list[list] = []   # open `case`s: [state, depth]; state is subject, in, pattern or body
        fresh = False        # the next word is the first of a case pattern, where esac ends the case
        groups = 0           # open { } brace groups, whose } is not the close of a ${ cmd; }

        def command_position() -> bool:
            return all(w in _RESERVED_LEADING or _ASSIGNMENT.match(w) for w in words)

        def end_word() -> None:
            nonlocal val, started, target, fresh, groups
            if started:
                if target:
                    target = False
                else:
                    w = "".join(val)
                    first = command_position()
                    words.append(w)
                    state = cases[-1][0] if cases else None
                    if state == "subject":
                        cases[-1][0] = "in"
                    elif state == "in":
                        if w == "in":
                            cases[-1][0], fresh = "pattern", True
                    elif state == "pattern":
                        if fresh and w == "esac":
                            cases.pop()
                        fresh = False
                    elif first and w == "esac" and state == "body":
                        cases.pop()
                    elif first and w == "case":
                        cases.append(["subject", depth])
                    elif first and w == "{":
                        groups += 1
            val, started = [], False

        def in_pattern() -> bool:
            return bool(cases) and cases[-1][0] == "pattern" and cases[-1][1] == depth

        def end_segment(end: int, next_start: int) -> None:
            nonlocal words, seg_start
            end_word()
            self.items.append((seg_start, end, words))
            words, seg_start = [], next_start

        while i < n:
            i = self._skip(i)
            if i >= n:
                break
            c = s[i]
            if c == "\\":
                if i + 1 < n:
                    val.append(s[i + 1])
                started = True; i += 2
            elif c in " \t":
                end_word(); i += 1
            elif c == "\n":
                end_segment(i, i + 1)
                i += 1
                if pending:
                    if all(p[3] == self.newlines for p in pending):
                        i = self._bodies(i, [p[:3] for p in pending])
                    pending, seg_start = [], i
                self.newlines += 1
            elif c == "#" and not started:
                j = s.find("\n", i)
                j = n if j < 0 else j
                self.comments.append((i, j)); i = j
            elif c == ")":
                end_word()
                if in_pattern():                  # the ) that ends a case pattern, not a subshell or $( close
                    cases[-1][0] = "body"
                    end_segment(i + 1, i + 1); i += 1
                    continue
                if stop == ")" and depth == 0:
                    end_segment(i, i + 1)
                    return i + 1
                depth = max(depth - 1, 0)
                end_segment(i + 1, i + 1); i += 1
            elif c == "(" and not started and fresh and in_pattern():
                fresh = False; i += 1             # the optional ( before a case pattern
            elif c == "(":
                j = self._match(i, "((")
                if j is not None and not started and all(w in _RESERVED_LEADING for w in words):
                    end = self._arith(j, "))")
                    if end is not None:
                        val.append(s[i:end]); started = True; i = end
                        continue
                end_segment(i, i); depth += 1; i += 1
            elif c == "&" and (j := self._match(i, "&>")) is not None:
                end_word(); target = True
                i = self._match(i, "&>>") or j
            elif c in ";&|":
                for op in _LIST_OPS:
                    if (j := self._match(i, op)) is not None:
                        end_segment(i, j); i = j
                        if op in (";;", ";&", ";;&") and cases and cases[-1][0] == "body":
                            cases[-1][0], fresh = "pattern", True
                        break
            elif c in "<>":
                if (j := self._match(i, c + "(")) is not None:
                    self.substitution = True
                    end = self._list(j, stop=")")
                    val.append(s[i:end]); started = True; i = end
                    continue
                if started and not target and _FD_WORD.match("".join(val)):
                    val, started = [], False      # 2>x: the number belongs to the redirection
                else:
                    end_word()
                if (j := self._match(i, "<<<")) is not None:
                    target = True; i = j
                elif (j := self._match(i, "<<")) is not None:
                    dash = self._match(j, "-")
                    j = dash or j
                    while (j := self._skip(j)) < n and s[j] in " \t":
                        j += 1
                    end, term, quoted = self._delimiter(j)
                    if end > j:
                        pending.append((term, dash is not None, quoted, self.newlines))
                    i = end
                else:
                    for op in _REDIRECT_OPS:
                        if (j := self._match(i, op)) is not None:
                            target = True; i = j
                            break
            elif c == "}" and not started and groups and command_position() and (stop == "}" or self._closes_word(i + 1)):
                groups -= 1                       # the } of a brace group: a word, as before. Inside ${ cmd; } a
                                                  # joined }x is taken as the group's too, so a later } closes
                val.append(c); started = True; i += 1
            elif c == "}" and stop == "}" and not started and not groups and all(w in _RESERVED_LEADING for w in words):
                end_segment(i, i + 1)             # the } that closes a ${ cmd; }: where a reserved word may appear,
                return i + 1                      # and (bash manual) it may be joined to the rest of the word
            elif c == "'":
                j = s.find("'", i + 1)
                j = n if j < 0 else j
                val.append(s[i + 1 : j]); self.single.append((i, min(j + 1, n)))
                started = True; i = j + 1
            elif c == '"':
                started = True; i = self._dquote(i + 1, val)
            elif c == "`":
                started = True
                j = self._backtick(i + 1); val.append(s[i:j]); i = j
            elif c == "$":
                started = True
                nxt = self._skip(i + 1)
                c1 = s[nxt : nxt + 1]
                if c1 == "$":
                    val.append("$$"); i = nxt + 1
                elif c1 == "'":
                    end = self._ansi_end(nxt + 1)
                    val.append(_ansi_c(s[nxt + 1 : end - 1])); self.single.append((i, end)); i = end
                elif c1 == '"':
                    i = self._dquote(nxt + 1, val)
                elif (j := self._dollar(i)) is not None:
                    val.append(s[i:j]); i = j
                else:
                    val.append("$"); i += 1
            else:
                val.append(c); started = True; i += 1
        end_segment(n, n)
        return n

    # -- results ------------------------------------------------------------------------------------------------

    def _drop(self) -> bytearray:
        drop = bytearray(self.n + 1)
        for k in self.removed:
            drop[k] = drop[k + 1] = 1
        return drop

    def _segments(self) -> list[_Segment]:
        drop = self._drop()
        for a, b in self.comments:
            drop[a:b] = b"\x01" * (b - a)
        out = []
        for item in self.items:
            if isinstance(item, _Segment):
                out.append(item)
            else:
                a, b, words = item
                out.append(_Segment("".join(self.s[k] for k in range(a, b) if not drop[k]), words))
        return [seg for seg in out if seg.text.strip()]

    def result(self) -> _Lexed:
        segments = self._segments()
        drop = self._drop()
        keep = [not drop[k] for k in range(self.n)]
        joined = "".join(c for c, ok in zip(self.s, keep) if ok)
        in_body = bytearray(self.n)
        for a, b in self.bodies:
            in_body[a:b] = b"\x01" * (b - a)
        stripped = "".join(c for k, (c, ok) in enumerate(zip(self.s, keep)) if ok and not in_body[k])
        masked, last = [], 0
        for a, b in sorted(self.single):
            if a < last:
                continue
            masked.append("".join(self.s[k] for k in range(last, a) if keep[k])); masked.append("''"); last = b
        masked.append("".join(self.s[k] for k in range(last, self.n) if keep[k]))
        return _Lexed(joined, stripped, "".join(masked), segments, self.substitution)


def _lex(cmd: str) -> _Lexed:
    lexer = _Lexer(cmd)
    lexer._list(0)
    return lexer.result()


def _split_segments(cmd: str) -> list[str]:
    """The text of every simple command bash would run, in order (commands inside substitutions included)."""
    return [seg.text for seg in _lex(cmd).segments]


def _strip_heredocs(cmd: str) -> str:
    """The command without here-document bodies (an unterminated body is kept, so it is checked as commands)."""
    return _lex(cmd).stripped


def _normalise_git(segment: str) -> str:
    """`git --no-pager -C x log` -> `git log`, so prefix rules see the verb."""
    return _GIT_GLOBAL_OPTS.sub(r"\1\2", segment, count=1)


_SEQUENCE = re.compile(r"^(?:(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z]))(?:\.\.(-?\d+))?$")


def _brace_expand(word: str, limit: int = 256) -> list[str]:
    """Bash brace expansion of one word: {a,b} and {x..y[..step]}, nested, preamble and postscript kept; a ${ is
    not a brace. Quotes are already removed from the lexer's words, so a quoted brace is expanded too (fail closed)."""
    i = 0
    while i < len(word):
        if word[i] == "{" and word[i - 1 : i] != "$":
            depth, commas, j = 0, [], i
            for j in range(i, len(word)):
                if word[j] == "{":
                    depth += 1
                elif word[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif word[j] == "," and depth == 1:
                    commas.append(j)
            if depth == 0:
                body = word[i + 1 : j]
                parts: list[str] = []
                if commas:
                    edges = [i, *commas, j]
                    parts = [word[a + 1 : b] for a, b in zip(edges, edges[1:])]
                elif m := _SEQUENCE.match(body):
                    step = abs(int(m.group(5) or 1)) or 1
                    num = m.group(1) is not None
                    a, b = (int(m.group(1)), int(m.group(2))) if num else (ord(m.group(3)), ord(m.group(4)))
                    seq = itertools.islice(range(a, b + (1 if b >= a else -1), step if b >= a else -step), limit)
                    parts = [str(k) if num else chr(k) for k in seq]
                if parts:
                    out: list[str] = []
                    for p in parts:
                        out.extend(_brace_expand(word[:i] + p + word[j + 1 :], limit))
                        if len(out) >= limit:
                            break
                    return [w for w in out if w][:limit] or [""]
        i += 1
    return [word]


def _command_words(words: list[str]) -> list[str]:
    """The words from the command word on: brace expansion applied, then reserved words such as ! { if then do,
    assignments, wrappers with their options (and the options' arguments) skipped; `env -S STRING` splits STRING."""
    words = [e for w in words for e in _brace_expand(w)]
    i = 0
    while i < len(words):
        name = words[i].rsplit("/", 1)[-1]
        if name in _WRAPPERS:   # before reserved words: `time -p` is both
            i += 1
            while i < len(words) and words[i].startswith("-"):
                opt = words[i]; i += 1
                if opt == "--":
                    break
                split = None
                if name == "env" and opt in ("-S", "--split-string") and i < len(words):
                    split = words[i]; i += 1
                elif name == "env" and opt.startswith("--split-string="):
                    split = opt.split("=", 1)[1]
                elif name == "env" and opt.startswith("-S"):
                    split = opt[2:]
                if split is not None:
                    segs = _lex(split).segments
                    return _command_words((segs[0].words if segs else []) + words[i:])
                if opt in _WRAPPER_ARG_OPTS.get(name, ()):
                    i += 1
            i += _WRAPPER_POSITIONAL.get(name, 0)
        elif words[i] in _RESERVED_LEADING or _ASSIGNMENT.match(words[i]):
            i += 1
        else:
            break
    return words[i:]


def _is_rm(words: list[str]) -> bool:
    """True for any rm invocation: the command word after quote removal (the lexer's words) and brace expansion,
    seen through reserved words, assignments, wrappers and their options, and paths."""
    cw = _command_words(words)
    return bool(cw) and cw[0].rsplit("/", 1)[-1] == "rm"


def _inner_command(words: list[str]) -> str | None:
    """The literal text an `eval` or a `bash -c` / `sh -c` ... would run as shell code, else None. A non-literal
    argument ($x, $(...)) is re-lexed as written; what it expands to at run time is a residual."""
    cw = _command_words(words)
    if not cw:
        return None
    name = cw[0].rsplit("/", 1)[-1]
    if name == "eval":
        return " ".join(cw[1:])
    if name not in _SHELLS:
        return None
    dash_c, j = False, 1
    while j < len(cw):
        a = cw[j]
        if a == "--":
            j += 1
            break
        if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            j += 2
            continue
        if len(a) > 1 and a[0] in "-+":
            dash_c = dash_c or (a[0] == "-" and not a.startswith("--") and "c" in a[1:])
            j += 1
            continue
        break
    return cw[j] if dash_c and j < len(cw) else None


def _cd_args(words: list[str]) -> list[str] | None:
    """The arguments of a cd or pushd that this simple command runs, else None (not a cd)."""
    cw = _command_words(words)
    return cw[1:] if cw and cw[0] in ("cd", "pushd") else None


def _allow_listed(words: list[str]) -> bool:
    """The lexed words (quotes removed, git's global options skipped) start with an allow-listed command."""
    if words[:1] == ["git"]:
        rest, k = words[1:], 0
        while k < len(rest) and rest[k].startswith("-"):
            k += 2 if rest[k] in ("-c", "-C") else 1
        words = ["git", *rest[k:]]
    return any(words[: len(p.split())] == p.split() for p in INVESTIGATE_ALLOWED_PREFIXES)


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
    positional: list[str] = []
    skip = False
    for t in tokens[1:]:
        if skip:
            skip = False
            continue
        if t in ("-f", "-s", "-w"):
            skip = True
            continue
        if not t.startswith("-") or t == "-":
            positional.append(t)
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
    cwds: set[Path] = field(default_factory=set)  # directories INVESTIGATE has cd'd into (the Bash tool keeps its cwd between calls)
    _pending_cwds: list[Path] = field(default_factory=list, repr=False)  # cd targets of the command being checked

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

    def _bases(self) -> list[Path]:
        """Every directory a relative path in a command might be resolved from."""
        return [self.worktree, *sorted(self.cwds), *self._pending_cwds]

    def _candidates(self, candidate: Path) -> list[Path]:
        """The paths a token may name: itself if absolute, else resolved from every base, both textually (bash -L) and physically."""
        if candidate.is_absolute():
            return [candidate]
        joined = [base / candidate for base in self._bases()]
        return [*joined, *(Path(os.path.normpath(j)) for j in joined)]

    def _under_home_not_scratch(self, file_path: str) -> bool:
        """True when a path resolves into ATeam's home but not into this worker's scratch dir."""
        home = self.ateam_home or Path(os.environ.get("ATEAM_HOME") or (Path.home() / ".ateam"))
        raw = file_path.strip().strip("'\"")
        if raw.startswith("~"):
            raw = os.path.expanduser(raw)
        raw = raw.replace("$HOME", str(Path.home())).replace("${HOME}", str(Path.home()))
        raw = raw.replace("$ATEAM_HOME", str(home)).replace("${ATEAM_HOME}", str(home))
        candidate = Path(raw)
        candidates = self._candidates(candidate)
        for cand in candidates:
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            try:
                resolved.relative_to(home.resolve())
            except ValueError:
                continue
            if not self._in_scratch(str(resolved)):
                return True
        return False

    def _wildcard_reaches_home(self, token: str) -> bool:
        """A wildcard token whose literal directory part is ATeam's home or one of its ancestors could expand into it."""
        m = _WILDCARD.search(token)
        if not m:
            return False
        literal = token[: m.start()]
        directory = literal.rsplit("/", 1)[0] if "/" in literal else "."
        home = (self.ateam_home or Path(os.environ.get("ATEAM_HOME") or (Path.home() / ".ateam"))).resolve()
        raw = directory.strip().strip("'\"")
        if raw.startswith("~"):
            raw = os.path.expanduser(raw)
        raw = raw.replace("$HOME", str(Path.home())).replace("${HOME}", str(Path.home()))
        raw = raw.replace("$ATEAM_HOME", str(home)).replace("${ATEAM_HOME}", str(home))
        candidate = Path(raw)
        candidates = self._candidates(candidate)
        for cand in candidates:
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved == home or resolved in home.parents or self._under_home_not_scratch(str(resolved)):
                return True
        return False

    def _token_paths(self, token: str) -> list[str]:
        """Path-like pieces of a shell token, including the value side of --flag=value and -Cpath forms."""
        tok = token.strip().strip("'\"")
        pieces: list[str] = []
        if tok.startswith("-"):
            if "=" in tok:
                pieces.append(tok.split("=", 1)[1])
            elif len(tok) > 2 and tok[1] in "CI" and tok[2] not in "-":
                pieces.append(tok[2:])
        else:
            pieces.append(tok)
        return [p for p in pieces if "/" in p or p.startswith((".", "~", "$"))]

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

    def _cd_target(self, seg: _Segment) -> tuple[Path, Path] | None:
        """(logical, physical) forms of the directory a plain `cd <abs path>` / `pushd <abs path>` names, else None.

        Every role (owner, 2026-10-26). Absolute paths only: the Bash tool keeps its working directory between
        calls and the policy never knows it, so a relative target (or a `..` after a symlinked cd) cannot be judged.
        Exactly one argument, no redirection, no expansion; both the textual (bash `cd -L`) and the resolved form
        must be an existing directory inside the worktree.
        """
        args = _cd_args(seg.words)
        if args is None or len(args) != 1 or re.search(r"[<>]", seg.text):
            return None
        arg = args[0]
        if not arg.startswith("/") or _WILDCARD.search(arg) or "$" in arg or "`" in arg:
            return None
        logical, physical = Path(os.path.normpath(arg)), Path(arg).resolve()
        if not (self._inside_worktree(logical) and self._inside_worktree(physical)):
            return None
        return logical, physical

    def _inside_worktree(self, path: Path) -> bool:
        return path.is_dir() and any(r == path or r in path.parents for r in {self.worktree, self.worktree.resolve()})

    def _all_segments(self, segments: list[_Segment], depth: int = 0) -> list[_Segment] | None:
        """The segments plus, after each, the segments of the literal shell code it runs through eval or a shell's
        -c (re-lexed, recursively). None when that nests deeper than _NESTING_LIMIT."""
        out: list[_Segment] = []
        for seg in segments:
            out.append(seg)
            inner = _inner_command(seg.words)
            if inner is not None:
                if depth >= _NESTING_LIMIT:
                    return None
                nested = self._all_segments(_lex(inner).segments, depth + 1)
                if nested is None:
                    return None
                out.extend(nested)
        return out

    def command_allowed(self, cmd: str) -> tuple[bool, str]:
        lexed = _lex(cmd)
        segments = self._all_segments(lexed.segments)
        if segments is None:
            return False, NESTING_REASON
        self._pending_cwds = []
        for seg in segments:
            if _cd_args(seg.words) is not None and (target := self._cd_target(seg)) is not None:
                self._pending_cwds.extend(target)
        try:
            ok, reason = self._command_allowed(lexed, segments)
            if ok:
                self.cwds.update(self._pending_cwds)
            return ok, reason
        finally:
            self._pending_cwds = []

    def _command_allowed(self, lexed: _Lexed, all_segments: list[_Segment]) -> tuple[bool, str]:
        for pattern in DESTRUCTIVE_PATTERNS:
            if re.search(pattern, lexed.joined):
                return False, f"destructive command matched {pattern!r}"
        if self.role == Role.investigate and lexed.substitution:
            return False, "INVESTIGATE may not use command or process substitution"
        scratch_spellings = self._spellings(self.scratch_dir)
        home_spellings = self._home_spellings() if self.scratch_dir is not None or self.ateam_home is not None else []
        for seg, words in ((s.text, s.words) for s in all_segments):
            mentions_scratch = any(sp in seg for sp in scratch_spellings)
            if mentions_scratch and ".." in seg:
                return False, "paths that leave your scratch directory are not allowed"
            if any(sp in seg for sp in home_spellings) and not mentions_scratch:
                return False, "ATeam's home (intent repo, state store, logs) is off limits to workers; only your scratch directory is writable"
            if home_spellings:
                for tok in seg.replace('"', "").replace("'", "").split():
                    for piece in self._token_paths(tok):
                        if _WILDCARD.search(piece):
                            if self._wildcard_reaches_home(piece):
                                return False, "wildcard paths that could reach ATeam's home are not allowed"
                        elif self._under_home_not_scratch(piece):
                            return False, "ATeam's home (intent repo, state store, logs) is off limits to workers; only your scratch directory is writable"
            rest = seg
            for sp in [*scratch_spellings, *home_spellings]:
                rest = rest.replace(sp, " ")
            if _is_rm(words):
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
        for s in all_segments:   # every role (owner, 2026-10-26)
            if _cd_args(s.words) is not None and self._cd_target(s) is None:
                return False, CD_WORKTREE_REASON
        if self.role == Role.investigate:
            if _PAGER.search(lexed.masked):
                return False, "INVESTIGATE may not set pagers, editors or diff drivers"
            for s in lexed.segments:
                seg = s.text
                if _CD.match(seg) and s.words[:1] in (["cd"], ["pushd"]):
                    continue   # a plain cd, judged above; any other spelling still meets the allow-list
                if _ENV_PREFIX.match(seg):
                    return False, f"INVESTIGATE may not set environment variables for a command; {seg.strip()[:40]!r}"
                if _GIT_DASH_C.match(seg):
                    return False, "INVESTIGATE may not pass git -c"
                norm = _normalise_git(seg)
                if not (any(_starts_with_prefix(norm, p) for p in INVESTIGATE_ALLOWED_PREFIXES) and _allow_listed(s.words)):
                    reason = f"INVESTIGATE may only run read-only inspection commands; {seg.strip()[:40]!r} is not allowed"
                    if seg.split()[:1] and seg.split()[0] in _READ_TOOL_HINT_WORDS:
                        reason += "; use the Read tool with offset and limit to read a line range"
                    return False, reason
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
            if tool_name == "Glob":
                pattern = str(tool_input.get("pattern") or "")
                base = Path(target) if target else self.worktree
                m = _WILDCARD.search(pattern)
                literal = pattern[: m.start()] if m else pattern
                literal_dir = literal.rsplit("/", 1)[0] if "/" in literal else ""
                probe = (Path(literal_dir) if Path(literal_dir).is_absolute() else base / literal_dir) if literal_dir else base
                if self._under_home_not_scratch(str(probe)) or (m and self._wildcard_reaches_home(str(probe / "x*"))):
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
        return {"PreToolUse": [HookMatcher(matcher="Edit|Write|MultiEdit|NotebookEdit|Bash|Read|Glob|Grep", hooks=[self.pre_tool_use])]}
