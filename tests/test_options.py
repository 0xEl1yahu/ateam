from ateam.roles import ROLE_SPECS, Role
from ateam.runner.options import clean_env, worker_options
from ateam.tiers import TierSpec


# The SDK starts the CLI with {**os.environ, **options.env} (claude_agent_sdk/_internal/transport/subprocess_cli.py):
# a key left out of options.env is inherited, so a scrubbed key is set to "" explicitly.

def test_clean_env_keeps_login_state_and_sets_its_own(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/elsewhere")
    monkeypatch.setenv("PATH", "/usr/bin")
    e = clean_env()
    assert e["CLAUDE_CONFIG_DIR"] == "/tmp/elsewhere" and e["PATH"] == "/usr/bin"
    assert e["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and e["SHELL"] == e["CLAUDE_CODE_SHELL"] == "/bin/bash"


def test_clean_env_strips_cdpath_and_startup_files(monkeypatch):
    # bash reads BASH_ENV in non-interactive shells, and ENV in sh mode: either could redefine cd or rm
    monkeypatch.setenv("CDPATH", "/tmp:/")
    monkeypatch.setenv("BASH_ENV", "/tmp/funcs.sh")
    monkeypatch.setenv("ENV", "/tmp/funcs.sh")
    e = clean_env()
    assert e["CDPATH"] == "" and e["BASH_ENV"] == "" and e["ENV"] == ""


def test_clean_env_blanks_startup_files_even_when_not_inherited(monkeypatch):
    for k in ("CDPATH", "BASH_ENV", "ENV"):
        monkeypatch.delenv(k, raising=False)
    e = clean_env()
    assert e["CDPATH"] == "" and e["BASH_ENV"] == "" and e["ENV"] == ""


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


def test_clean_env_runs_workers_under_bash(monkeypatch, policy_build, tmp_path):
    # The Bash tool otherwise runs the login shell (zsh on macOS); the policy models bash grammar. CLAUDE_CODE_SHELL overrides
    # Claude Code's own shell detection, which need not follow SHELL; both are set after the CLAUDE* scrub. Owner, 2026-10-26.
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("CLAUDE_CODE_SHELL", "/bin/zsh")
    e = clean_env()
    assert e["SHELL"] == "/bin/bash" and e["CLAUDE_CODE_SHELL"] == "/bin/bash"
    o = worker_options(cwd=tmp_path, spec=ROLE_SPECS[Role.build], brief_text="B", policy=policy_build, tier=TierSpec("haiku", "low", 10, 0.5))
    assert o.env["SHELL"] == "/bin/bash" and o.env["CLAUDE_CODE_SHELL"] == "/bin/bash"
