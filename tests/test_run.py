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


def test_prepare_scratch_outside_worktree(target_repo, target_paths, tmp_path):
    from ateam.gitops import GitRepo
    from ateam.runner.run import prepare_scratch

    scratch = prepare_scratch(target_paths, "C-0001")
    assert scratch == target_paths.scratch_root / "C-0001" and scratch.is_dir()
    assert prepare_scratch(target_paths, "C-0001") == scratch  # idempotent
    assert target_repo.resolve() not in scratch.resolve().parents
    (scratch / "handoff.yaml").write_text("progress: x\n")
    assert GitRepo(target_repo).is_clean()


@pytest.mark.asyncio
async def test_worktree_has_no_ateam_files_after_run(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.config import TargetConfig
    from ateam.contracts.select import freeze
    from ateam.gitops import GitRepo
    from ateam.paths import home_dir
    from ateam.roles import Role
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB
    from ateam.tiers import load_tiers

    async def fake_query(prompt, options):
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    contract = freeze(sample_contract, [records_three["R-0001"]])
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    git = GitRepo(target_repo)
    res, tree, scope, cp = await run_mod.run_contract_attempt(
        paths=target_paths, config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
        contract=contract, records=records_three, role=Role.build, tiers=load_tiers(), db=db,
    )
    assert not (target_repo / ".ateam-scratch").exists() and not (target_repo / ".ateam").exists()
    assert git.is_clean() and scope.ok
    assert (target_paths.scratch_root / "C-0001").is_dir()

@pytest.mark.asyncio
async def test_attempt_fails_as_tamper_when_intent_repo_changes_during_run(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.config import TargetConfig
    from ateam.contracts.select import freeze
    from ateam.gitops import GitRepo
    from ateam.paths import home_dir
    from ateam.roles import Role
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB
    from ateam.tiers import load_tiers

    async def fake_query(prompt, options):
        (target_paths.records / "R-0099-planted.yaml").write_text("id: R-0099\n")  # a worker reaching the intent repo
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    contract = freeze(sample_contract, [records_three["R-0001"]])
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    git = GitRepo(target_repo)
    res, tree, scope, cp = await run_mod.run_contract_attempt(
        paths=target_paths, config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
        contract=contract, records=records_three, role=Role.build, tiers=load_tiers(), db=db,
    )
    row = db.telemetry_rows()[-1]
    assert row["outcome"] == "tamper" and row["failure_class"] == "tamper"


# ---- setup runs once per worktree

def _setup_db(tmp_path):
    from ateam.state.db import StateDB

    db = StateDB(tmp_path / "setup-state.db")
    db.init_schema()
    return db


def test_ensure_setup_runs_commands_once_per_worktree(target_repo, ateam_home, tmp_path):
    from ateam.config import TargetConfig
    from ateam.runner.run import ensure_setup, setup_key

    db = _setup_db(tmp_path)
    cfg = TargetConfig(setup=["touch setup-ran", "echo second"], env={"ATEAM_X": "1"})
    ran = ensure_setup(target_repo, cfg, db, "b")
    assert ran == cfg.setup and (target_repo / "setup-ran").exists()
    assert db.get_meta(setup_key(target_repo, "b"))
    (target_repo / "setup-ran").unlink()
    assert ensure_setup(target_repo, cfg, db, "b") == [] and not (target_repo / "setup-ran").exists()


def test_ensure_setup_passes_config_env(target_repo, ateam_home, tmp_path):
    from ateam.config import TargetConfig
    from ateam.runner.run import ensure_setup, setup_key

    cfg = TargetConfig(setup=["echo $ATEAM_X > env-out"], env={"ATEAM_X": "seen"})
    ensure_setup(target_repo, cfg, _setup_db(tmp_path), "b")
    assert (target_repo / "env-out").read_text().strip() == "seen"


def test_ensure_setup_with_no_commands_records_nothing(target_repo, ateam_home, tmp_path):
    from ateam.config import TargetConfig
    from ateam.runner.run import ensure_setup, setup_key

    db = _setup_db(tmp_path)
    assert ensure_setup(target_repo, TargetConfig(), db, "b") == []
    assert db.get_meta(setup_key(target_repo, "b")) is None


def test_ensure_setup_failure_records_nothing(target_repo, ateam_home, tmp_path):
    from ateam.config import TargetConfig
    from ateam.runner.run import SetupError, ensure_setup, setup_key

    db = _setup_db(tmp_path)
    cfg = TargetConfig(setup=["echo before", "echo boom >&2; exit 3", "echo never"])
    with pytest.raises(SetupError) as e:
        ensure_setup(target_repo, cfg, db, "b")
    assert e.value.cmd == "echo boom >&2; exit 3" and e.value.returncode == 3 and "boom" in e.value.output
    assert db.get_meta(setup_key(target_repo, "b")) is None


@pytest.mark.parametrize("verdicts,flag,expected", [
    ([("A1", "met")], False, ("completed", None)),
    ([("A1", "not_met")], False, ("failed", "implementation")),
    ([("A1", "not_met")], True, ("failed", "requirements")),
    ([("A1", "cannot_determine")], True, ("completed", None)),
    ([("A1", "met")], True, ("completed", None)),
])
def test_classify_test_report(verdicts, flag, expected):
    from ateam.runner.run import classify_test_report

    report = {"verdicts": [{"id": i, "verdict": v, "evidence": "e"} for i, v in verdicts], "notes": "", "requirements_unclear": flag}
    assert classify_test_report(report) == expected


@pytest.mark.asyncio
async def test_test_attempt_records_classification(monkeypatch, target_repo, sample_contract, records_three, ateam_home):
    from ateam.config import TargetConfig
    from ateam.contracts.select import freeze
    from ateam.gitops import GitRepo
    from ateam.paths import TargetPaths, home_dir
    from ateam.roles import Role
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB
    from ateam.tiers import load_tiers

    report = {"verdicts": [{"id": "A1", "verdict": "not_met", "evidence": "x"}], "notes": "A1 is ambiguous", "requirements_unclear": True}

    async def fake_query(prompt, options):
        yield _result(structured_output=report)

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    contract = freeze(sample_contract, [records_three["R-0001"]])
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    git = GitRepo(target_repo)
    await run_mod.run_contract_attempt(
        paths=TargetPaths.from_root(target_repo), config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
        contract=contract, records=records_three, role=Role.test, tiers=load_tiers(), db=db,
    )
    row = db.telemetry_rows()[-1]
    assert row["outcome"] == "failed" and row["failure_class"] == "requirements"


# Items 9 and 19: the setup marker follows the worktree, not only its path.
def test_ensure_setup_runs_again_for_a_worktree_recreated_at_the_same_path(target_repo, ateam_home, tmp_path):
    from ateam.config import TargetConfig
    from ateam.gitops import GitRepo
    from ateam.runner.run import ensure_setup, setup_key

    db = _setup_db(tmp_path)
    cfg = TargetConfig(setup=["touch setup-ran"])
    wt = tmp_path / "wt-again"
    g = GitRepo(target_repo)
    g.worktree_add(wt, "wt-again", "main")
    first = setup_key(wt, "b")
    assert ensure_setup(wt, cfg, db, "b") == cfg.setup
    assert ensure_setup(wt, cfg, db, "b") == []
    g.run("worktree", "remove", "--force", str(wt))
    g.run("branch", "-D", "wt-again")
    g.worktree_add(wt, "wt-again", "main")
    assert setup_key(wt, "b") != first and not (wt / "setup-ran").exists()
    assert ensure_setup(wt, cfg, db, "b") == cfg.setup and (wt / "setup-ran").exists()


# Item 20: a setup command that hangs is a SetupError, not a hung run.
def test_ensure_setup_times_out(target_repo, ateam_home, tmp_path, monkeypatch):
    import ateam.runner.run as run_mod
    from ateam.config import TargetConfig

    monkeypatch.setattr(run_mod, "SETUP_TIMEOUT_S", 0.5)
    db = _setup_db(tmp_path)
    with pytest.raises(run_mod.SetupError) as e:
        run_mod.ensure_setup(target_repo, TargetConfig(setup=["sleep 5"]), db, "b")
    assert e.value.cmd == "sleep 5" and "timed out after 0.5 s" in e.value.output
    assert db.get_meta(run_mod.setup_key(target_repo, "b")) is None


def test_setup_timeout_is_ten_minutes():
    from ateam.runner.run import SETUP_TIMEOUT_S
    assert SETUP_TIMEOUT_S == 600


# ---- TEST gets its own scratch; BUILD retries read BUILD notes (plan 2026-10-26-scheduler, task 2)

def test_test_role_scratch_is_separate_from_build(target_paths):
    from ateam.roles import Role
    from ateam.runner.run import prepare_scratch
    build = prepare_scratch(target_paths, "C-0001")
    test = prepare_scratch(target_paths, "C-0001", Role.test)
    assert build == target_paths.scratch_root / "C-0001" and test == target_paths.scratch_root / "C-0001-test"
    assert test.is_dir() and test.parent == build.parent      # same depth: build_policy's home derivation holds
    assert prepare_scratch(target_paths, "C-0001", Role.investigate) == build


def _attempt_harness(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.contracts.select import freeze
    from ateam.paths import home_dir
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB

    async def fake_query(prompt, options):
        yield _result()

    seen: dict[str, list] = {"scratch": [], "prev": []}
    real_policy, real_brief = run_mod.build_policy, run_mod.assemble_brief

    def spy_policy(*a, **kw):
        seen["scratch"].append(kw["scratch_dir"]); return real_policy(*a, **kw)

    def spy_brief(contract, selected, prev, role, **kw):
        seen["prev"].append(prev); return real_brief(contract, selected, prev, role, **kw)

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    monkeypatch.setattr(run_mod, "build_policy", spy_policy)
    monkeypatch.setattr(run_mod, "assemble_brief", spy_brief)
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    contract = freeze(sample_contract, [records_three["R-0001"]])
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    return run_mod, db, contract, seen


async def _attempt(run_mod, target_repo, target_paths, contract, records_three, db, role):
    from ateam.config import TargetConfig
    from ateam.gitops import GitRepo
    from ateam.tiers import load_tiers
    git = GitRepo(target_repo)
    return await run_mod.run_contract_attempt(
        paths=target_paths, config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
        contract=contract, records=records_three, role=role, tiers=load_tiers(), db=db)


@pytest.mark.asyncio
async def test_test_attempt_never_sees_build_handoff(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.roles import Role
    run_mod, db, contract, seen = _attempt_harness(monkeypatch, target_repo, target_paths, sample_contract, records_three)
    build_scratch = target_paths.scratch_root / contract.id
    build_scratch.mkdir(parents=True, exist_ok=True)
    (build_scratch / "handoff.yaml").write_text("progress: built it my way\n")
    res, tree, scope, cp = await _attempt(run_mod, target_repo, target_paths, contract, records_three, db, Role.test)
    assert seen["scratch"] == [target_paths.scratch_root / f"{contract.id}-test"]
    assert cp.notes is None


@pytest.mark.asyncio
async def test_build_retry_brief_uses_build_notes(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.roles import Role
    run_mod, db, contract, seen = _attempt_harness(monkeypatch, target_repo, target_paths, sample_contract, records_three)
    build_scratch = target_paths.scratch_root / contract.id
    build_scratch.mkdir(parents=True, exist_ok=True)
    (build_scratch / "handoff.yaml").write_text("progress: half done\n")
    await _attempt(run_mod, target_repo, target_paths, contract, records_three, db, Role.build)
    await _attempt(run_mod, target_repo, target_paths, contract, records_three, db, Role.test)
    await _attempt(run_mod, target_repo, target_paths, contract, records_three, db, Role.build)
    assert seen["prev"][2] is not None and seen["prev"][2].notes == {"progress": "half done"}


@pytest.mark.asyncio
async def test_build_attempt_brief_carries_only_live_guidance(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from datetime import UTC, datetime

    from ateam.guidance import GuidanceEntry, append_guidance
    from ateam.roles import Role
    run_mod, db, contract, seen = _attempt_harness(monkeypatch, target_repo, target_paths, sample_contract, records_three)
    briefs: list = []
    spied = run_mod.assemble_brief

    def capture(*a, **kw):
        briefs.append(kw.get("instructions")); return spied(*a, **kw)

    monkeypatch.setattr(run_mod, "assemble_brief", capture)
    at = datetime(2026, 10, 27, tzinfo=UTC)
    append_guidance(target_paths.guidance, contract.id, GuidanceEntry(at=at, source="owner", contract_hash=contract.content_hash(), text="live"))
    append_guidance(target_paths.guidance, contract.id, GuidanceEntry(at=at, source="owner", contract_hash="stale", text="old"))
    await _attempt(run_mod, target_repo, target_paths, contract, records_three, db, Role.build)
    await _attempt(run_mod, target_repo, target_paths, contract, records_three, db, Role.test)
    assert [e.text for e in briefs[0]] == ["live"] and list(briefs[1]) == []


def test_setup_key_covers_the_base(target_repo, ateam_home, tmp_path):
    """Owner ruling 10 (2026-10-27): a lane worktree runs setup again for each new base, once per base."""
    from ateam.config import TargetConfig
    from ateam.runner.run import ensure_setup

    db = _setup_db(tmp_path)
    cfg = TargetConfig(setup=["echo x >> setup-count"])
    assert ensure_setup(target_repo, cfg, db, "base-one") == cfg.setup
    assert ensure_setup(target_repo, cfg, db, "base-one") == []
    assert ensure_setup(target_repo, cfg, db, "base-two") == cfg.setup
    assert (target_repo / "setup-count").read_text().count("x") == 2
