"""Throwaway spike: what the Claude Agent SDK + headless Claude Code actually do.

Run from the ATeam repo:
    .venv/bin/python spikes/2026-10-24-sdk-bare-hooks/run_spike.py

Builds a scratch git repo and worktrees under SCRATCH, runs the numbered
checks from the design's "The spike" section, and writes
spikes/2026-10-24-sdk-bare-hooks/report.md plus raw JSON under out/.
Nothing here is product code.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SCRATCH = Path(
    os.environ.get(
        "ATEAM_SPIKE_SCRATCH",
        "/private/tmp/claude-501/-Users-elibelilty-Documents-GitHub-skooly/"
        "034d3617-8c99-4966-abcd-b82a1dc4394d/scratchpad/ateam-spike",
    )
)
TARGET = SCRATCH / "target"
WORKTREES = SCRATCH / "worktrees"
MODEL = os.environ.get("ATEAM_SPIKE_MODEL", "haiku")

RULE_TOKEN = "RULE-TOKEN-7731"
CLAUDE_MD_TOKEN = "CLAUDEMD-TOKEN-4410"
APPEND_TOKEN = "APPEND-TOKEN-9052"
UPS_TOKEN = "UPS-TOKEN-2268"
AGENT_TOKEN = "AGENT-TOKEN-5519"

REPORT: list[str] = []
RAW: dict[str, Any] = {}


def log(line: str = "") -> None:
    print(line, flush=True)
    REPORT.append(line)


def sh(cmd: list[str], cwd: Path | None = None, check: bool = True, strip: bool = True) -> str:
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {r.stderr}")
    # Lesson for the builder: never strip `git status --porcelain`; its first
    # column is significant (" M path" lost its leading space in the first run
    # and produced a bogus "rc/app.py").
    return r.stdout.strip() if strip else r.stdout.rstrip("\n")


# ---------------------------------------------------------------- scratch repo


def make_target() -> None:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    TARGET.mkdir(parents=True)
    WORKTREES.mkdir(parents=True)
    sh(["git", "init", "-q", "-b", "main"], cwd=TARGET)
    sh(["git", "config", "user.email", "spike@example.invalid"], cwd=TARGET)
    sh(["git", "config", "user.name", "spike"], cwd=TARGET)
    (TARGET / "src").mkdir()
    (TARGET / "src" / "app.py").write_text(
        "def greet(name):\n    return f'hello {name}'\n"
    )
    (TARGET / "tests").mkdir()
    (TARGET / "tests" / "test_app.py").write_text(
        "from src.app import greet\n\ndef test_greet():\n    assert greet('x') == 'hello x'\n"
    )
    (TARGET / ".ateam" / "contracts").mkdir(parents=True)
    (TARGET / ".ateam" / "contracts" / "C-0001.yaml").write_text(
        "id: C-0001\ntitle: original title\n"
    )
    (TARGET / ".claude" / "rules").mkdir(parents=True)
    (TARGET / ".claude" / "rules" / "app-scope.md").write_text(
        "---\npaths:\n  - \"src/**\"\n---\n"
        f"Scoped rule marker: {RULE_TOKEN}. If you see this marker, you must report it.\n"
    )
    (TARGET / "CLAUDE.md").write_text(
        f"Project memory marker: {CLAUDE_MD_TOKEN}. If you see this marker, you must report it.\n"
    )
    sh(["git", "add", "-A"], cwd=TARGET)
    sh(["git", "commit", "-q", "-m", "scratch target"], cwd=TARGET)


def make_worktree(name: str) -> Path:
    path = WORKTREES / name
    if path.exists():
        sh(["git", "worktree", "remove", "--force", str(path)], cwd=TARGET, check=False)
        shutil.rmtree(path, ignore_errors=True)
    sh(["git", "worktree", "add", "-q", "-b", f"wt-{name}", str(path), "main"], cwd=TARGET)
    # untracked scratch dir, excluded per-worktree
    git_dir = Path(sh(["git", "rev-parse", "--git-dir"], cwd=path))
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "exclude").write_text(".ateam-scratch/\n")
    (path / ".ateam-scratch").mkdir()
    return path


def tree_state(path: Path) -> dict[str, Any]:
    return {
        "porcelain": sh(["git", "status", "--porcelain", "--untracked-files=all"], cwd=path, strip=False),
        "head": sh(["git", "rev-parse", "--short", "HEAD"], cwd=path),
        "diff_names": sh(["git", "diff", "--name-only", "main"], cwd=path),
    }


# ---------------------------------------------------------------- SDK helpers


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "failed"]},
        "saw_rule_token": {"type": "boolean"},
        "saw_claude_md_token": {"type": "boolean"},
        "saw_append_token": {"type": "boolean"},
        "saw_ups_token": {"type": "boolean"},
        "saw_agent_token": {"type": "boolean"},
        "edited_protected_file": {"type": "boolean"},
        "denial_message": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["status", "saw_rule_token", "saw_claude_md_token", "notes"],
    "additionalProperties": True,
}

TOKEN_PROMPT = (
    "Task: read the file src/app.py with the Read tool. Then finish.\n"
    "In your final structured report, set each saw_* flag to true only if the "
    "corresponding marker string appeared anywhere in your instructions, system "
    "prompt, injected context, or tool results during this session: "
    f"{RULE_TOKEN} (saw_rule_token), {CLAUDE_MD_TOKEN} (saw_claude_md_token), "
    f"{APPEND_TOKEN} (saw_append_token), {UPS_TOKEN} (saw_ups_token), "
    f"{AGENT_TOKEN} (saw_agent_token). Do not search the filesystem for the markers; "
    "only report what you were shown. Put a one-line summary in notes."
)


def base_options(cwd: Path, **over: Any) -> ClaudeAgentOptions:
    kw: dict[str, Any] = dict(
        cwd=str(cwd),
        env=clean_env(),
        model=MODEL,
        effort="low",
        max_turns=10,
        max_budget_usd=0.60,
        permission_mode="acceptEdits",
        can_use_tool=allow_all,
        output_format={"type": "json_schema", "schema": REPORT_SCHEMA},
        tools=["Read", "Glob", "Grep", "Bash", "Edit", "Write"],
    )
    kw.update(over)
    return ClaudeAgentOptions(**kw)


PERMISSION_CALLS: list[dict[str, Any]] = []


async def allow_all(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
    PERMISSION_CALLS.append({"tool": tool_name, "input": _short(input_data)})
    return PermissionResultAllow()


def _short(d: Any) -> Any:
    s = json.dumps(d, default=str)
    return s if len(s) < 300 else s[:300] + "…"


async def run(name: str, prompt: str, options: ClaudeAgentOptions) -> dict[str, Any]:
    """Run one query; return a dict with result fields, tool uses and timing."""
    t0 = time.time()
    tool_uses: list[dict[str, Any]] = []
    msg_types: list[str] = []
    init: dict[str, Any] = {}
    result: ResultMessage | None = None
    error: str | None = None
    try:
        async for m in query(prompt=prompt, options=options):
            msg_types.append(type(m).__name__)
            if isinstance(m, SystemMessage) and m.subtype == "init":
                init = {k: m.data.get(k) for k in ("model", "permissionMode", "tools", "agents", "plugins", "claude_code_version", "cwd") if k in m.data}
            elif isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        tool_uses.append({"tool": b.name, "input": _short(b.input)})
            elif isinstance(m, ResultMessage):
                result = m
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    out: dict[str, Any] = {
        "name": name,
        "elapsed_s": round(time.time() - t0, 1),
        "error": error,
        "init": init,
        "msg_types": msg_types,
        "tool_uses": tool_uses,
    }
    if result is not None:
        out["result"] = dataclasses.asdict(result)
    RAW[name] = out
    return out


def summarize(r: dict[str, Any]) -> str:
    if r.get("error"):
        return f"ERROR {r['error']}"
    res = r.get("result") or {}
    so = res.get("structured_output")
    return (
        f"subtype={res.get('subtype')} is_error={res.get('is_error')} "
        f"turns={res.get('num_turns')} cost=${(res.get('total_cost_usd') or 0):.3f} "
        f"terminal={res.get('terminal_reason')} tools={[t['tool'] for t in r['tool_uses']]} "
        f"structured_output={json.dumps(so) if so is not None else None}"
    )


# ---------------------------------------------------------------- checks


async def check_0_bare_auth(wt: Path) -> bool:
    log("## 0. Does bare mode authenticate with claude.ai login?")
    r = await run("0_bare_auth", "Reply with the single word pong.", base_options(
        wt, extra_args={"bare": None}, output_format=None, tools=[], max_turns=1,
    ))
    log(summarize(r))
    ok = not r.get("error") and (r.get("result") or {}).get("subtype") == "success" and not (r.get("result") or {}).get("is_error")
    log(f"**bare mode usable with this login: {ok}**")
    log()
    return ok


def clean_room(bare_ok: bool) -> dict[str, Any]:
    """Flags that give a worker nothing from the target repo."""
    if bare_ok:
        return {"extra_args": {"bare": None}}
    return {
        "setting_sources": [],          # no user/project/local settings or hooks
        "strict_mcp_config": True,
        "extra_args": {"disable-slash-commands": None},
    }


async def check_1_2_context(wt: Path, bare_ok: bool) -> None:
    log("## 1 and 2. Structured output, and what context reaches a clean-room worker")
    cr = clean_room(bare_ok)

    r = await run("1_clean_room", TOKEN_PROMPT, base_options(wt, **cr))
    log(f"- clean room ({'bare' if bare_ok else 'no settings'}): {summarize(r)}")

    r = await run("2_normal", TOKEN_PROMPT, base_options(
        wt, setting_sources=["user", "project", "local"],
    ))
    log(f"- normal (user+project+local settings): {summarize(r)}")

    async def ups_hook(input_data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": f"Injected marker: {UPS_TOKEN}"}}

    r = await run("2b_clean_room_injection", TOKEN_PROMPT, base_options(
        wt,
        system_prompt={"type": "preset", "preset": "claude_code", "append": f"Appended marker: {APPEND_TOKEN}"},
        hooks={"UserPromptSubmit": [HookMatcher(hooks=[ups_hook])]},
        **cr,
    ))
    log(f"- clean room + append-system-prompt + UserPromptSubmit hook: {summarize(r)}")

    agents_json = json.dumps({
        "build": {
            "description": "BUILD role",
            "prompt": f"You are a BUILD worker. Agent marker: {AGENT_TOKEN}.",
            "tools": ["Read", "Glob", "Grep", "Bash", "Edit", "Write"],
            "omitClaudeMd": True,
        }
    })
    extra = dict(cr.get("extra_args", {}))
    extra.update({"agents": agents_json, "agent": "build"})
    cr2 = dict(cr)
    cr2["extra_args"] = extra
    r = await run("2c_agent_as_main", TOKEN_PROMPT, base_options(wt, **cr2))
    log(f"- clean room + --agents/--agent build (omitClaudeMd): {summarize(r)}")
    log(f"  init.agents={r.get('init', {}).get('agents')} init.tools={r.get('init', {}).get('tools')}")
    log()


async def check_3_pretooluse_deny(wt: Path, bare_ok: bool) -> None:
    log("## 3. PreToolUse hook denies edits under .ateam/")
    denials: list[str] = []

    async def deny_ateam(input_data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        fp = str((input_data.get("tool_input") or {}).get("file_path", ""))
        if "/.ateam/" in fp or fp.startswith(".ateam/"):
            denials.append(fp)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "ATeam: .ateam/ is protected; contracts are read-only for workers.",
            }}
        return {}

    prompt = (
        "Use the Edit tool to change the title field in .ateam/contracts/C-0001.yaml "
        "from 'original title' to 'HACKED'. If the edit is denied, do not try any other "
        "tool or method; stop and report. Set edited_protected_file accordingly and put "
        "any denial text in denial_message."
    )
    r = await run("3_deny_edit", prompt, base_options(
        wt, hooks={"PreToolUse": [HookMatcher(matcher="Edit|Write|MultiEdit", hooks=[deny_ateam])]}, **clean_room(bare_ok),
    ))
    log(f"- {summarize(r)}")
    log(f"- hook denials fired for: {denials}")
    log(f"- file after run: {(wt / '.ateam/contracts/C-0001.yaml').read_text().strip()!r}")
    log(f"- tree: {tree_state(wt)}")
    log()


async def check_4_stop_block(wt: Path, bare_ok: bool) -> None:
    log("## 4. Stop hook blocks until a condition holds")
    stop_calls: list[dict[str, Any]] = []
    done = wt / "DONE.txt"

    async def stop_gate(input_data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        stop_calls.append({"stop_hook_active": input_data.get("stop_hook_active"), "done_exists": done.exists()})
        if done.exists() or len(stop_calls) > 3:
            return {}
        return {"decision": "block", "reason": "ATeam gate: before stopping, create DONE.txt in the repo root containing the word done, then stop."}

    r = await run("4_stop_block", "Say hello in one sentence and stop.", base_options(
        wt, hooks={"Stop": [HookMatcher(hooks=[stop_gate])]}, output_format=None, **clean_room(bare_ok),
    ))
    log(f"- {summarize(r)}")
    log(f"- stop hook calls: {stop_calls}")
    log(f"- DONE.txt exists: {done.exists()}")
    log()


async def check_5_tree_check(wt: Path, bare_ok: bool) -> None:
    log("## 5. A shell write outside declared paths is caught by the tree check")
    prompt = (
        "Using the Bash tool only, run: mkdir -p notes && echo scratch > notes/outside.txt . "
        "Then also append a comment line '# touched' to src/app.py with the Edit tool. "
        "Do not commit. Then finish with status done."
    )
    r = await run("5_tree_check", prompt, base_options(wt, **clean_room(bare_ok)))
    log(f"- {summarize(r)}")
    ts = tree_state(wt)
    log(f"- tree after run: {ts}")
    declared = {"src/app.py"}
    touched = {line[3:] for line in ts["porcelain"].splitlines() if line.strip()}
    outside = sorted(p for p in touched if p not in declared)
    log(f"- outside declared paths: {outside}  -> scope breach detected: {bool(outside)}")
    log()


async def check_6_envelope() -> None:
    log("## 6. Result envelope fields actually present")
    res = (RAW.get("1_clean_room") or {}).get("result") or {}
    present = {k: (v if k in ("subtype", "is_error", "num_turns", "terminal_reason", "stop_reason", "total_cost_usd", "duration_ms", "api_error_status") else ("<set>" if v not in (None, [], {}) else None)) for k, v in res.items()}
    log("```json\n" + json.dumps(present, indent=2, default=str) + "\n```")
    log(f"- usage keys: {sorted((res.get('usage') or {}).keys())}")
    log(f"- model_usage: {json.dumps(res.get('model_usage'), default=str)[:400]}")
    log(f"- permission callback saw {len(PERMISSION_CALLS)} calls; sample: {PERMISSION_CALLS[:3]}")
    log()


async def check_7_concurrency(bare_ok: bool) -> None:
    log("## 7. Two concurrent queries from one process")
    a, b = make_worktree("conc-a"), make_worktree("conc-b")
    t0 = time.time()
    ra, rb = await asyncio.gather(
        run("7_conc_a", TOKEN_PROMPT, base_options(a, **clean_room(bare_ok))),
        run("7_conc_b", TOKEN_PROMPT, base_options(b, **clean_room(bare_ok))),
    )
    log(f"- a: {summarize(ra)}")
    log(f"- b: {summarize(rb)}")
    log(f"- wall {round(time.time() - t0, 1)}s vs sum {ra['elapsed_s'] + rb['elapsed_s']}s; distinct sessions: "
        f"{(ra.get('result') or {}).get('session_id') != (rb.get('result') or {}).get('session_id')}")
    log()


async def check_8_context_signals(wt: Path, bare_ok: bool) -> None:
    log("## 8. Context and lifecycle signals: usage per message, PreCompact, resume after max_turns")
    precompact: list[Any] = []

    async def pc(input_data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        precompact.append(input_data.get("trigger"))
        return {}

    usage_seen: list[Any] = []
    prompt = (
        "Do these steps one tool call at a time: 1) Read src/app.py. 2) Read tests/test_app.py. "
        "3) Run `ls -R` with Bash. 4) Write a file .ateam-scratch/handoff.yaml with keys progress and next. "
        "Then report status done."
    )
    opts = base_options(wt, max_turns=2, hooks={"PreCompact": [HookMatcher(hooks=[pc])]}, output_format=None, **clean_room(bare_ok))
    first: ResultMessage | None = None
    try:
        async for m in query(prompt=prompt, options=opts):
            if isinstance(m, AssistantMessage):
                u = getattr(m, "usage", None)
                if u:
                    usage_seen.append(_short(u))
            elif isinstance(m, ResultMessage):
                first = m
    except Exception as e:  # noqa: BLE001
        log(f"- first run error: {e}")
    if first:
        log(f"- capped run: subtype={first.subtype} terminal={first.terminal_reason} turns={first.num_turns} is_error={first.is_error}")
        log(f"- per-message usage on AssistantMessage: {usage_seen[:2] or 'none exposed'}")
        log(f"- PreCompact fired: {precompact}")
        r2 = await run("8_resume", "Continue the task where you left off, then report status done.", base_options(
            wt, resume=first.session_id, max_turns=8, output_format=None, **clean_room(bare_ok),
        ))
        log(f"- resume of capped session: {summarize(r2)}")
        log(f"- handoff.yaml exists after resume: {(wt / '.ateam-scratch/handoff.yaml').exists()}")
    log()


# ---------------------------------------------------------------- main


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    make_target()
    log("# Spike report: SDK, bare mode, hooks, rules, worktrees")
    log(f"Date: {time.strftime('%Y-%m-%d %H:%M')}. Model alias: {MODEL}. SDK: {__import__('claude_agent_sdk').__version__}. ")
    log(f"Scratch: {SCRATCH}")
    log()
    wt = make_worktree("main-checks")
    bare_ok = await check_0_bare_auth(wt)
    await check_1_2_context(wt, bare_ok)
    await check_3_pretooluse_deny(make_worktree("deny"), bare_ok)
    await check_4_stop_block(make_worktree("stop"), bare_ok)
    await check_5_tree_check(make_worktree("tree"), bare_ok)
    await check_6_envelope()
    await check_7_concurrency(bare_ok)
    await check_8_context_signals(make_worktree("signals"), bare_ok)
    total = sum(((r.get("result") or {}).get("total_cost_usd") or 0) for r in RAW.values())
    log(f"Total estimated cost: ${total:.2f} across {len(RAW)} runs.")
    (HERE / "report.md").write_text("\n".join(REPORT) + "\n")
    (OUT / "raw.json").write_text(json.dumps(RAW, indent=2, default=str))
    print(f"\nwrote {HERE / 'report.md'} and {OUT / 'raw.json'}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
