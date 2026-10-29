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



@pytest.mark.asyncio
async def test_a_recorded_coordinator_commit_during_the_session_is_not_tamper(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    """A merge or refresh the scheduler made beside the session (overlap gate design, owner 2026-10-28)."""
    from ateam.config import TargetConfig
    from ateam.contracts.select import freeze
    from ateam.gitops import GitRepo
    from ateam.intent import commit_intent_files, intent_git, record_coordinator_commits
    from ateam.paths import home_dir
    from ateam.roles import Role
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB
    from ateam.tiers import load_tiers

    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()

    async def fake_query(prompt, options):
        before = intent_git(target_paths).head()
        f = target_paths.intent / "coordinator.txt"; f.write_text("merged\n")
        commit_intent_files(target_paths, "ateam: integrate C-0002", [f])
        record_coordinator_commits(db, intent_git(target_paths), before)
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    contract = freeze(sample_contract, [records_three["R-0001"]])
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    git = GitRepo(target_repo)
    res, tree, scope, cp = await run_mod.run_contract_attempt(
        paths=target_paths, config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
        contract=contract, records=records_three, role=Role.build, tiers=load_tiers(), db=db,
    )
    row = db.telemetry_rows()[-1]
    assert row["outcome"] != "tamper", (row, res.error)


@pytest.mark.asyncio
async def test_an_unrecorded_intent_commit_is_tamper_and_named(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.config import TargetConfig
    from ateam.contracts.select import freeze
    from ateam.gitops import GitRepo
    from ateam.intent import commit_intent_files
    from ateam.paths import home_dir
    from ateam.roles import Role
    from ateam.runner import run as run_mod
    from ateam.state.db import StateDB
    from ateam.tiers import load_tiers

    async def fake_query(prompt, options):
        f = target_paths.intent / "planted.txt"; f.write_text("x\n")
        commit_intent_files(target_paths, "ateam: integrate C-0009", [f])   # looks like ATeam's, but nobody recorded it
        yield _result()

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    monkeypatch.setattr("ateam.runner.run.INTENT_GRACE_S", 0.05)
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    contract = freeze(sample_contract, [records_three["R-0001"]])
    db.upsert_contract(contract.id, contract.plan, contract.status, contract.content_hash())
    git = GitRepo(target_repo)
    res, tree, scope, cp = await run_mod.run_contract_attempt(
        paths=target_paths, config=TargetConfig(), git_worktree=git, worktree=target_repo, base=git.head(),
        contract=contract, records=records_three, role=Role.build, tiers=load_tiers(), db=db,
    )
    row = db.telemetry_rows()[-1]
    assert row["outcome"] == "tamper" and "ateam: integrate C-0009" in (res.error or ""), (row, res.error)

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


def test_build_policy_lets_a_worker_write_its_incidental_paths(target_repo, sample_contract, records_three):
    """Spec, worker roles: BUILD may not touch files outside `paths` plus `incidental`, so an incidental path is
    writable. P-0019 C-0083 was refused tests/test_planner.py, its incidental path, and paused (owner, 2026-10-28)."""
    from ateam.config import TargetConfig
    from ateam.roles import Role
    from ateam.runner.run import build_policy

    policy = build_policy(target_repo, Role.build, sample_contract, TargetConfig(), records_three)
    assert policy.path_writable_abs(str(target_repo / "requirements.txt"))[0]          # incidental
    assert policy.path_writable_abs(str(target_repo / "src" / "api" / "x.py"))[0]      # declared
    assert not policy.path_writable_abs(str(target_repo / "src" / "app.py"))[0]        # neither


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [True, False])
async def test_no_result_cost_is_unknown(monkeypatch, policy_build, raises):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[TextBlock(text="hi")], model="haiku")
        if raises:
            raise RuntimeError("connection lost")

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert r.subtype == "no_result" and r.cost_usd is None

    async def with_result(prompt, options):
        yield _result(total_cost_usd=0.25)

    monkeypatch.setattr("ateam.runner.run.query", with_result)
    assert (await run_worker("p", ClaudeAgentOptions(), policy_build)).cost_usd == 0.25


# ---- C-0097: the event log

def _events_msgs(big=None):
    from claude_agent_sdk import ToolResultBlock, UserMessage
    out = big if big is not None else "file contents"
    return [
        AssistantMessage(content=[TextBlock(text="looking"), ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"})], model="haiku", message_id="m1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=out, is_error=False)], tool_use_result={"secret": "RAWRESULT"}),
        _result(),
    ]


async def _logged_attempt(monkeypatch, target_repo, target_paths, sample_contract, records_three, msgs, exc=None):
    from ateam.roles import Role
    run_mod, db, contract, seen = _attempt_harness(monkeypatch, target_repo, target_paths, sample_contract, records_three)

    async def fq(prompt, options):
        for m in msgs:
            yield m
        if exc:
            raise exc

    monkeypatch.setattr("ateam.runner.run.query", fq)
    res = await _attempt(run_mod, target_repo, target_paths, contract, records_three, db, Role.build)
    return db, contract, res


def _read(target_paths, n=1):
    from ateam.events import read_events
    return read_events(target_paths.logs / f"A-{n:04d}.jsonl")


@pytest.mark.asyncio
async def test_fake_worker_run_writes_gapless_parseable_log(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    db, contract, _ = await _logged_attempt(monkeypatch, target_repo, target_paths, sample_contract, records_three, _events_msgs())
    evs = _read(target_paths)
    row = db.attempt(1)
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
    assert [e.type for e in evs] == ["session_start", "assistant_text", "tool_call", "tool_result", "report", "session_end"]
    assert evs[0].payload["started_at"] == row["started_at"]
    assert all(e.attempt == 1 and e.contract == row["contract_id"] == contract.id and e.role == row["role"] for e in evs)


@pytest.mark.asyncio
async def test_every_written_payload_has_exactly_its_types_keys(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.events import PAYLOAD_KEYS
    msgs = _events_msgs()[:2] + [AssistantMessage(content=[TextBlock(text="x")], model="h", error="rate_limit"), _result()]
    await _logged_attempt(monkeypatch, target_repo, target_paths, sample_contract, records_three, msgs)
    evs = _read(target_paths)
    assert evs and all(set(e.payload) == PAYLOAD_KEYS[e.type] for e in evs)
    await _logged_attempt(monkeypatch, target_repo, target_paths, sample_contract, records_three, msgs[:1], exc=RuntimeError("x"))
    evs = _read(target_paths, 2)
    assert evs[-1].type == "error" and all(set(e.payload) == PAYLOAD_KEYS[e.type] for e in evs)


@pytest.mark.asyncio
async def test_error_event_only_for_a_stream_exception(monkeypatch, policy_build, tmp_path):
    from ateam.events import EventLog, read_events
    msgs = {"exc": [AssistantMessage(content=[TextBlock(text="hi")], model="h")],
            "none": [AssistantMessage(content=[TextBlock(text="hi")], model="h", error="rate_limit")]}
    results = {}
    for name, exc in (("exc", RuntimeError("boom")), ("none", None)):
        async def fq(prompt, options, name=name, exc=exc):
            for m in msgs[name]:
                yield m
            if exc:
                raise exc
        monkeypatch.setattr("ateam.runner.run.query", fq)
        log = EventLog(tmp_path / f"{name}.jsonl", 1, "C-1", "build", "t")
        results[name] = await run_worker("p", ClaudeAgentOptions(), policy_build, log)
        log.close()
    exc_ev = read_events(tmp_path / "exc.jsonl")
    assert [e.type for e in exc_ev] == ["session_start", "assistant_text", "error"] and "boom" in exc_ev[-1].payload["error"]
    none_ev = read_events(tmp_path / "none.jsonl")
    assert [e.type for e in none_ev] == ["session_start", "assistant_text", "session_end"]
    assert none_ev[1].payload["sdk_error"] == "rate_limit" and none_ev[-1].payload["subtype"] == "no_result"
    assert results["none"].subtype == "no_result" and results["none"].error is None and results["exc"].error == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_full_tool_output_never_reaches_the_log(monkeypatch, target_repo, target_paths, sample_contract, records_three):
    from ateam.events import TOOL_RESULT_LIMIT
    big = "".join(chr(65 + i % 26) for i in range(TOOL_RESULT_LIMIT + 500))
    await _logged_attempt(monkeypatch, target_repo, target_paths, sample_contract, records_three, _events_msgs(big))
    raw = (target_paths.logs / "A-0001.jsonl").read_text()
    assert big not in raw and "RAWRESULT" not in raw
    tr = [e for e in _read(target_paths) if e.type == "tool_result"][0].payload
    assert tr["truncated"] and tr["length"] == len(big) and len(tr["content"]) == TOOL_RESULT_LIMIT and len(tr["sha256"]) == 64


@pytest.mark.asyncio
async def test_worker_killed_mid_stream_leaves_readable_partial_log(tmp_path):
    import subprocess
    import sys
    import textwrap
    from ateam.events import read_events
    log_path = tmp_path / "A-0001.jsonl"
    code = textwrap.dedent(f'''
        import asyncio, os, signal
        from pathlib import Path
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock
        from ateam.events import EventLog
        from ateam.roles import Role
        from ateam.runner import run as run_mod
        from ateam.runner.policy import Policy

        async def fq(prompt, options):
            yield AssistantMessage(content=[TextBlock(text="one")], model="h")
            yield AssistantMessage(content=[TextBlock(text="two")], model="h")
            os.kill(os.getpid(), signal.SIGKILL)

        run_mod.query = fq
        pol = Policy(worktree=Path("."), role=Role.build, allowed_paths=[], protected=[])
        log = EventLog(Path({str(log_path)!r}), 1, "C-1", "build", "t")
        asyncio.run(run_mod.run_worker("p", ClaudeAgentOptions(), pol, log))
    ''')
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode != 0
    evs = read_events(log_path)
    assert [e.seq for e in evs] == [1, 2, 3] and [e.type for e in evs] == ["session_start", "assistant_text", "assistant_text"]
