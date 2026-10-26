"""The clean-room session options a worker runs with. Verified by the 2026-10-24 spike."""

from __future__ import annotations

import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from ateam.roles import RoleSpec
from ateam.runner.policy import Policy
from ateam.tiers import TierSpec


# Keys the scrub blanks. The SDK starts the CLI with {**os.environ, **options.env}
# (claude_agent_sdk/_internal/transport/subprocess_cli.py), so leaving a key out of options.env inherits it: every
# scrubbed key is set to "" explicitly. CDPATH would redirect a bare `cd name`; BASH_ENV (non-interactive bash) and ENV
# (sh mode) name startup files that could redefine cd or rm.
_BLANKED = ("CDPATH", "BASH_ENV", "ENV")


def clean_env() -> dict[str, str]:
    """The parent environment with CDPATH, BASH_ENV and ENV set to "", plus the variables ATeam sets on purpose:
    auto-memory disabled and the Bash tool on /bin/bash (the policy models bash grammar; the login shell may be zsh).
    CLAUDE* variables are inherited (the worker needs the parent's login state) except the two that mark a nested
    Claude Code session: the SDK drops CLAUDECODE only from the inherited environment, not from options.env."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.update({k: "" for k in _BLANKED})
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
