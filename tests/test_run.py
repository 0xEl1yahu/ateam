import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultError, ResultMessage, TextBlock, ToolUseBlock

from ateam.runner.run import WorkerResult, run_worker


def _result(**over):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s1",
                total_cost_usd=0.01, usage={"input_tokens": 5, "output_tokens": 3}, structured_output={"status": "done"})
    base.update(over)
    return ResultMessage(**base)


@pytest.mark.asyncio
async def test_run_worker_collects_result(monkeypatch, policy_build):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="hi"), ToolUseBlock(id="t1", name="Read", input={"file_path": "src/api/x.py"})], model="haiku")
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert isinstance(r, WorkerResult)
    assert r.report == {"status": "done"} and r.session_id == "s1" and r.subtype == "success" and not r.is_error
    assert r.tool_uses == [{"tool": "Read", "input": {"file_path": "src/api/x.py"}}]
    assert r.cost_usd == 0.01 and r.usage["input_tokens"] == 5 and r.num_turns == 2 and r.error is None and r.wall_s >= 0


@pytest.mark.asyncio
async def test_run_worker_keeps_result_on_result_error(monkeypatch, policy_build):
    async def fake_query(prompt, options):
        yield _result(subtype="error_max_turns", is_error=True, structured_output=None)
        raise ResultError("boom", {"subtype": "error_max_turns"}, exit_code=1)

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert r.subtype == "error_max_turns" and r.is_error and r.report is None and r.error and "boom" in r.error


@pytest.mark.asyncio
async def test_run_worker_without_result(monkeypatch, policy_build):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="hi")], model="haiku")
        raise RuntimeError("connection lost")

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert r.subtype == "no_result" and r.is_error and r.session_id is None and "connection lost" in r.error


@pytest.mark.asyncio
async def test_attempt_is_finished_even_if_post_run_fails(monkeypatch, target_repo, sample_contract, records_three, ateam_home):
    from ateam.config import TargetConfig
    from ateam.contracts.select import freeze
    from ateam.gitops import GitRepo
    from ateam.paths import TargetPaths, home_dir
    from ateam.roles import Role
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB
    from ateam.tiers import load_tiers

    async def fake_query(prompt, options):
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    monkeypatch.setattr(run_mod, "collect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git exploded")))
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    contract = freeze(sample_contract.model_copy(update={"allocation": sample_contract.allocation.model_copy(update={"tier": {"build": "light", "test": "light", "investigate": "none"}})}), [records_three["R-0001"]])
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    git = GitRepo(target_repo)
    with pytest.raises(RuntimeError, match="git exploded"):
        await run_mod.run_contract_attempt(
            paths=TargetPaths.from_root(target_repo), config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
            contract=contract, records=records_three, role=Role.build, tiers=load_tiers(), db=db,
        )
    row = db.telemetry_rows()[-1]
    assert row["outcome"] == "failed" and row["failure_class"] == "environment" and row["finished_at"]


@pytest.mark.asyncio
async def test_tier_override_is_recorded_without_touching_contract(monkeypatch, target_repo, sample_contract, records_three, ateam_home):
    from ateam.config import TargetConfig
    from ateam.contracts.select import freeze
    from ateam.gitops import GitRepo
    from ateam.paths import TargetPaths, home_dir
    from ateam.roles import Role
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB
    from ateam.tiers import load_tiers

    seen = {}

    async def fake_query(prompt, options):
        seen["model"] = options.model
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    contract = freeze(sample_contract, [records_three["R-0001"]])   # build tier is "standard" (sonnet)
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    git = GitRepo(target_repo)
    res, tree, scope, cp = await run_mod.run_contract_attempt(
        paths=TargetPaths.from_root(target_repo), config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
        contract=contract, records=records_three, role=Role.build, tiers=load_tiers(), db=db, tier_override="light",
    )
    row = db.telemetry_rows()[-1]
    assert seen["model"] == "haiku" and row["tier"] == "light" and row["model"] == "haiku"
    assert cp.contract_hash == contract.content_hash() == db.contract_hash("C-0001")


def test_prepare_worktree_requires_tracked_gitignore(target_repo, tmp_path):
    from ateam.gitops import GitRepo
    from ateam.runner.run import WorktreeNotPrepared, prepare_worktree

    wt = tmp_path / "wt"
    g = GitRepo(target_repo)
    g.worktree_add(wt, "wt-prep", "main")
    prepare_worktree(wt)
    prepare_worktree(wt)  # idempotent
    (wt / ".ateam-scratch/handoff.yaml").write_text("progress: x\n")
    assert (wt / ".ateam-scratch").is_dir() and GitRepo(wt).is_clean()
    common = (wt / GitRepo(wt).run("rev-parse", "--git-common-dir").strip()).resolve()
    exclude = common / "info/exclude"
    assert not exclude.exists() or ".ateam-scratch" not in exclude.read_text()

    (target_repo / ".gitignore").write_text("__pycache__/\n")
    g.run("add", ".gitignore"); g.run("commit", "-q", "-m", "drop ignore")
    wt2 = tmp_path / "wt2"; g.worktree_add(wt2, "wt-prep2", "main")
    with pytest.raises(WorktreeNotPrepared, match=".gitignore"):
        prepare_worktree(wt2)
    assert not (wt2 / ".ateam-scratch").exists()
