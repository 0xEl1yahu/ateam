"""Follow-up checks after run_spike.py. Reuses its scratch repo and helpers.

    .venv/bin/python spikes/2026-10-24-sdk-bare-hooks/run_followup.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_spike as S  # noqa: E402
from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny  # noqa: E402

CLEAN = {"setting_sources": [], "strict_mcp_config": True, "extra_args": {"disable-slash-commands": None}}


async def check_a_project_settings() -> None:
    S.log("## A. Do generated rules and CLAUDE.md load with setting_sources=['project'] only?")
    wt = S.make_worktree("proj-only")
    r = await S.run("A_project_only", S.TOKEN_PROMPT, S.base_options(
        wt, setting_sources=["project"], strict_mcp_config=True, extra_args={"disable-slash-commands": None},
    ))
    S.log(f"- {S.summarize(r)}")
    S.log()


async def check_b_agent_main() -> None:
    S.log("## B. --agent as the main agent: does it apply, and can it still emit structured output?")
    wt = S.make_worktree("agent-main")
    agents_json = json.dumps({"build": {
        "description": "BUILD role",
        "prompt": f"You are a BUILD worker. Agent marker: {S.AGENT_TOKEN}. Always finish by producing the structured output you were asked for.",
        "tools": ["Read", "Glob", "Grep", "Bash", "Edit", "Write"],
    }})
    extra = {"disable-slash-commands": None, "agents": agents_json, "agent": "build"}
    r = await S.run("B_agent_main", S.TOKEN_PROMPT, S.base_options(
        wt, setting_sources=[], strict_mcp_config=True, extra_args=extra, max_turns=8,
    ))
    S.log(f"- {S.summarize(r)}")
    S.log(f"- result text: {str((r.get('result') or {}).get('result'))[:300]!r}")
    S.log()


async def check_c_permission_callback() -> None:
    S.log("## C. Is can_use_tool invoked, and can it deny Bash? (permission_mode=default)")
    wt = S.make_worktree("perm-cb")
    calls: list[dict[str, Any]] = []

    async def gate(tool_name: str, input_data: dict[str, Any], ctx: Any) -> Any:
        calls.append({"tool": tool_name, "input": S._short(input_data)})
        if tool_name == "Bash" and "rm " in str(input_data.get("command", "")):
            return PermissionResultDeny(message="ATeam: destructive command denied.")
        return PermissionResultAllow()

    prompt = (
        "Using the Bash tool, first run `echo hi`, then run `rm -f nothing.txt`. "
        "If a command is denied, do not retry it another way; report what happened in notes and set status blocked."
    )
    r = await S.run("C_perm_cb", prompt, S.base_options(wt, permission_mode="default", can_use_tool=gate, **CLEAN))
    S.log(f"- {S.summarize(r)}")
    S.log(f"- callback calls: {calls}")
    S.log()


async def check_d_bash_hook() -> None:
    S.log("## D. PreToolUse hook on Bash denies commands that mention .ateam/ (permission_mode=acceptEdits)")
    wt = S.make_worktree("bash-hook")
    fired: list[str] = []

    async def deny_ateam_bash(input_data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        cmd = str((input_data.get("tool_input") or {}).get("command", ""))
        if ".ateam" in cmd:
            fired.append(cmd)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "ATeam: shell access to .ateam/ is denied."}}
        return {}

    prompt = (
        "Using the Bash tool only, run: echo 'title: HACKED' > .ateam/contracts/C-0001.yaml . "
        "If denied, do not try another way; set status blocked and put the denial text in denial_message."
    )
    r = await S.run("D_bash_hook", prompt, S.base_options(
        wt, hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[deny_ateam_bash])]}, **CLEAN,
    ))
    S.log(f"- {S.summarize(r)}")
    S.log(f"- hook fired for: {fired}")
    S.log(f"- file after run: {(wt / '.ateam/contracts/C-0001.yaml').read_text().strip()!r}")
    S.log()


async def main() -> None:
    if not S.TARGET.exists():
        S.make_target()
    S.log("# Spike follow-up")
    S.log()
    await check_a_project_settings()
    await check_b_agent_main()
    await check_c_permission_callback()
    await check_d_bash_hook()
    total = sum(((r.get("result") or {}).get("total_cost_usd") or 0) for r in S.RAW.values())
    S.log(f"Total estimated cost: ${total:.2f} across {len(S.RAW)} runs.")
    (S.HERE / "report-followup.md").write_text("\n".join(S.REPORT) + "\n")
    (S.OUT / "raw-followup.json").write_text(json.dumps(S.RAW, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
