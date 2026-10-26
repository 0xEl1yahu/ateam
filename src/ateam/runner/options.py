"""The clean-room session options a worker runs with. Verified by the 2026-10-24 spike."""

from __future__ import annotations

import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from ateam.roles import RoleSpec
from ateam.runner.policy import Policy
from ateam.tiers import TierSpec


def clean_env() -> dict[str, str]:
    """The parent environment minus every CLAUDE* variable, CDPATH, BASH_ENV and ENV, with auto-memory disabled and
    the Bash tool on /bin/bash (the policy models bash grammar; the login shell may be zsh)."""
    # CDPATH would redirect a bare `cd name`; BASH_ENV (non-interactive bash) and ENV (sh mode) name startup files
    # that could redefine cd or rm
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k not in ("CDPATH", "BASH_ENV", "ENV")}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["SHELL"] = env["CLAUDE_CODE_SHELL"] = "/bin/bash"   # CLAUDE_CODE_SHELL overrides Claude Code's shell detection
    return env


def worker_options(
    *, cwd: Path, spec: RoleSpec, brief_text: str, policy: Policy, tier: TierSpec, resume: str | None = None,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(cwd),
        env=clean_env(),
        setting_sources=[],
        strict_mcp_config=True,
        extra_args={"disable-slash-commands": None},
        permission_mode="default",
        can_use_tool=policy.can_use_tool,
        hooks=policy.hook_matchers(),
        tools=list(spec.tools),
        system_prompt={"type": "preset", "preset": "claude_code", "append": spec.system_prompt + "\n\n" + brief_text},
        output_format={"type": "json_schema", "schema": spec.report_schema},
        model=tier.model,
        effort=tier.effort,
        max_turns=tier.max_turns,
        max_budget_usd=tier.usd,
        resume=resume,
    )
