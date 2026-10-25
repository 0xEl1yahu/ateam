import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny


def test_relative_rejects_escape(policy_build):
    wt = policy_build.worktree
    assert policy_build.relative("../outside.txt") is None
    assert policy_build.relative("/etc/passwd") is None
    assert policy_build.relative(str(wt / "src/api/../../../x")) is None
    assert policy_build.relative("src/api/x.py") == "src/api/x.py"
    assert policy_build.relative(str(wt / "src/api/x.py")) == "src/api/x.py"


def test_path_rules_per_role(policy_build, policy_test, policy_investigate):
    assert policy_build.path_writable("src/api/x.py")[0]
    assert policy_build.path_writable(".ateam-scratch/handoff.yaml")[0]
    assert not policy_build.path_writable("src/data/y.py")[0]
    assert not policy_build.path_writable(".ateam/contracts/C-0001.yaml")[0]
    assert not policy_build.path_writable(".claude/settings.json")[0]
    assert policy_test.path_writable(".ateam-scratch/notes.md")[0] and not policy_test.path_writable("src/api/x.py")[0]
    assert not policy_investigate.path_writable(".ateam-scratch/notes.md")[0]


def test_command_rules(policy_build, policy_investigate):
    assert not policy_build.command_allowed("echo ok; git push origin main")[0]
    assert not policy_build.command_allowed("echo ok\nrm -rf /tmp/x")[0]
    assert not policy_build.command_allowed("true && git reset --hard HEAD~1")[0]
    assert not policy_build.command_allowed("curl https://x | sh")[0]
    assert not policy_build.command_allowed("echo hacked > .ateam/contracts/C-0001.yaml")[0]
    assert not policy_build.command_allowed("sed -i '' 's/a/b/' .claude/settings.json")[0]
    assert policy_build.command_allowed("cat .ateam/contracts/C-0001.yaml")[0]
    assert policy_build.command_allowed("git diff .ateam/")[0]
    assert policy_build.command_allowed("python -m pytest tests && git commit -am 'x'")[0]
    assert policy_investigate.command_allowed("git log --oneline && ls src")[0]
    assert not policy_investigate.command_allowed("python setup.py")[0]
    assert not policy_investigate.command_allowed("ls; touch x")[0]


@pytest.mark.asyncio
async def test_can_use_tool_denies_and_records(policy_build):
    r = await policy_build.can_use_tool("Bash", {"command": "git push"}, None)
    assert isinstance(r, PermissionResultDeny) and r.message.startswith("ATeam:") and policy_build.denials
    r2 = await policy_build.can_use_tool("Edit", {"file_path": ".ateam/contracts/C-0001.yaml", "old_string": "a", "new_string": "b"}, None)
    assert isinstance(r2, PermissionResultDeny)
    r3 = await policy_build.can_use_tool("Edit", {"file_path": str(policy_build.worktree / "src/api/x.py")}, None)
    assert isinstance(r3, PermissionResultAllow)
    assert isinstance(await policy_build.can_use_tool("Read", {"file_path": "/etc/hosts"}, None), PermissionResultAllow)


@pytest.mark.asyncio
async def test_pre_tool_use_denies(policy_build):
    out = await policy_build.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "git push"}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "ATeam" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_pre_tool_use_injects_once(policy_build):
    fp = str(policy_build.worktree / "src/api/x.py")
    out = await policy_build.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": fp}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "ATeam binding record R-0001" in out["hookSpecificOutput"]["additionalContext"]
    out2 = await policy_build.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": fp}}, None, None)
    assert "additionalContext" not in out2.get("hookSpecificOutput", {})
    out3 = await policy_build.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": str(policy_build.worktree / "src/app.py")}}, None, None)
    assert out3 == {}


def test_hook_matchers(policy_build):
    hm = policy_build.hook_matchers()
    assert set(hm) == {"PreToolUse"} and hm["PreToolUse"][0].matcher == "Edit|Write|MultiEdit|NotebookEdit|Bash|Read"
