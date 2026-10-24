"""Follow-up E: ATeam-owned path-scoped delivery via PreToolUse additionalContext.

    .venv/bin/python spikes/2026-10-24-sdk-bare-hooks/run_followup2.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_spike as S  # noqa: E402
from claude_agent_sdk import HookMatcher  # noqa: E402

CLEAN = {"setting_sources": [], "strict_mcp_config": True, "extra_args": {"disable-slash-commands": None}}


async def check_e_pretooluse_context() -> None:
    S.log("## E. PreToolUse hook injects a bound record when the worker reads a bound path (setting_sources=[])")
    wt = S.make_worktree("ptu-ctx")
    fired: list[str] = []

    async def inject(input_data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        fp = str((input_data.get("tool_input") or {}).get("file_path", ""))
        if fp.endswith("src/app.py"):
            fired.append(fp)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                           "additionalContext": f"Binding record for this file: {S.RULE_TOKEN}. Report it."}}
        return {}

    r = await S.run("E_ptu_context", S.TOKEN_PROMPT, S.base_options(
        wt, hooks={"PreToolUse": [HookMatcher(matcher="Read|Edit|Write", hooks=[inject])]}, **CLEAN,
    ))
    S.log(f"- {S.summarize(r)}")
    S.log(f"- hook fired for: {fired}")
    S.log()


async def main() -> None:
    if not S.TARGET.exists():
        S.make_target()
    S.log("# Spike follow-up 2")
    S.log()
    await check_e_pretooluse_context()
    (S.HERE / "report-followup2.md").write_text("\n".join(S.REPORT) + "\n")
    (S.OUT / "raw-followup2.json").write_text(json.dumps(S.RAW, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
