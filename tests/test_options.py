from ateam.roles import ROLE_SPECS, Role
from ateam.runner.options import clean_env, worker_options
from ateam.tiers import TierSpec


def test_clean_env_strips_claude(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    monkeypatch.setenv("PATH", "/usr/bin")
    e = clean_env()
    assert "CLAUDECODE" not in e and "CLAUDE_CODE_SESSION_ID" not in e
    assert e["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and e["PATH"] == "/usr/bin"


def test_worker_options_clean_room(policy_build, tmp_path):
    spec = ROLE_SPECS[Role.build]
    o = worker_options(cwd=tmp_path, spec=spec, brief_text="BRIEF", policy=policy_build, tier=TierSpec("haiku", "low", 10, 0.5))
    assert o.cwd == str(tmp_path)
    assert o.setting_sources == [] and o.strict_mcp_config is True
    assert o.extra_args == {"disable-slash-commands": None} and o.permission_mode == "default"
    assert o.system_prompt["type"] == "preset" and o.system_prompt["append"].endswith("BRIEF") and spec.system_prompt in o.system_prompt["append"]
    assert o.output_format == {"type": "json_schema", "schema": spec.report_schema}
    assert o.tools == spec.tools and o.can_use_tool == policy_build.can_use_tool and "PreToolUse" in o.hooks
    assert o.model == "haiku" and o.effort == "low" and o.max_turns == 10 and o.max_budget_usd == 0.5
    assert o.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and o.resume is None
    assert worker_options(cwd=tmp_path, spec=spec, brief_text="B", policy=policy_build, tier=TierSpec("haiku", "low", 10, 0.5), resume="s1").resume == "s1"
