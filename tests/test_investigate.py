import pytest
from claude_agent_sdk import ResultMessage

from ateam.config import TargetConfig
from ateam.gitops import GitRepo
from ateam.paths import home_dir
from ateam.roles import DISCOVERY_SCHEMA, DISCOVERY_SPEC, PLANNER_SCHEMA, PLANNER_SPEC, Role
from ateam.runner.investigate import InvestigationResult, run_investigation
from ateam.state.db import StateDB
from ateam.tiers import TierSpec


def _result(report):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s1",
                         total_cost_usd=0.01, usage={"input_tokens": 5, "output_tokens": 3}, structured_output=report)


def test_discovery_and_planner_specs_are_investigate_and_strict():
    for spec in (DISCOVERY_SPEC, PLANNER_SPEC):
        assert spec.role == Role.investigate and spec.tools == ["Read", "Glob", "Grep", "Bash"]
        assert spec.report_schema["additionalProperties"] is False
        assert "ATeam binding record" in spec.system_prompt
        assert "never modify files or state" in spec.system_prompt.lower()
    assert set(DISCOVERY_SCHEMA["properties"]) == {"candidates", "unknowns", "notes"}
    cand = DISCOVERY_SCHEMA["properties"]["candidates"]["items"]["properties"]
    assert set(cand) == {"kind", "statement", "rationale", "origin", "basis", "evidence", "paths", "serves", "confidence"}
    assert set(PLANNER_SCHEMA["properties"]) == {"title", "contracts", "proposed_records", "unknowns", "notes",
                                                         "depends_on_plans", "files_read", "out_of_scope", "verification_approach"}
    c = PLANNER_SCHEMA["properties"]["contracts"]["items"]["properties"]
    assert set(c) == {"title", "paths", "incidental", "depends_on", "acceptance", "checks", "allocation", "budget", "interfaces"}
    assert c["depends_on"]["items"]["type"] == "integer"


@pytest.fixture
def inv_env(target_repo, target_paths):
    db = StateDB(home_dir(target_repo) / "state.db"); db.init_schema()
    return dict(paths=target_paths, config=TargetConfig(), target_git=GitRepo(target_repo), db=db, tier=TierSpec("haiku", "low", 10, 0.5))


async def _run(inv_env, name="init", spec=DISCOVERY_SPEC):
    return await run_investigation(paths=inv_env["paths"], config=inv_env["config"], target_git=inv_env["target_git"], name=name,
                                   spec=spec, prompt="go", brief_text="BRIEF", tier=inv_env["tier"], db=inv_env["db"])


@pytest.mark.asyncio
async def test_run_investigation_runs_in_checkout_and_records_attempt(monkeypatch, inv_env, target_repo):
    seen = {}

    async def fake_query(prompt, options):
        seen["cwd"] = options.cwd; seen["tools"] = options.tools
        yield _result({"candidates": [], "unknowns": ["x"], "notes": "n"})

    monkeypatch.setattr("ateam.runner.investigate.query", fake_query, raising=False)
    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await _run(inv_env)
    assert isinstance(r, InvestigationResult) and r.res.report["unknowns"] == ["x"] and r.tampered == []
    assert seen["cwd"] == str(target_repo) and seen["tools"] == ["Read", "Glob", "Grep", "Bash"]
    git = inv_env["target_git"]
    assert git.is_clean() and "ateam/" not in git.run("branch", "--list") and not (home_dir(target_repo) / "worktrees").exists()
    row = inv_env["db"].telemetry_rows()[-1]
    assert row["contract_id"] == "init" and row["role"] == "investigate" and row["outcome"] == "completed" and row["attempt_id"] == r.attempt_id
    assert (inv_env["paths"].scratch_root / "init").is_dir()


@pytest.mark.asyncio
async def test_run_investigation_marks_tamper_when_checkout_changes(monkeypatch, inv_env, target_repo):
    async def fake_query(prompt, options):
        (target_repo / "planted.txt").write_text("x\n")
        yield _result({"candidates": [], "unknowns": [], "notes": "n"})

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await _run(inv_env)
    assert r.tampered and any("planted.txt" in t for t in r.tampered) and r.res.report is None
    assert inv_env["db"].telemetry_rows()[-1]["outcome"] == "tamper"


@pytest.mark.asyncio
async def test_run_investigation_marks_tamper_when_intent_changes(monkeypatch, inv_env):
    async def fake_query(prompt, options):
        (inv_env["paths"].records / "R-0099-planted.yaml").write_text("id: R-0099\n")
        yield _result({"candidates": [], "unknowns": [], "notes": "n"})

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await _run(inv_env)
    assert r.tampered and any("intent" in t for t in r.tampered) and r.res.report is None


@pytest.mark.asyncio
async def test_run_investigation_closes_attempt_on_exception(monkeypatch, inv_env):
    async def fake_query(prompt, options):
        yield _result({"candidates": [], "unknowns": [], "notes": "n"})

    async def boom(prompt, options, policy):
        raise RuntimeError("session exploded")

    monkeypatch.setattr("ateam.runner.investigate.run_worker", boom)
    with pytest.raises(RuntimeError):
        await _run(inv_env)
    rows = inv_env["db"].telemetry_rows()
    assert rows and rows[-1]["outcome"] == "failed" and rows[-1]["failure_class"] == "environment"


@pytest.mark.asyncio
async def test_run_investigation_passes_resume_and_starts_its_own_attempt(monkeypatch, inv_env):
    seen = []

    async def fake_query(prompt, options):
        seen.append(options.resume)
        yield _result({"candidates": [], "unknowns": [], "notes": "n"})

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    first = await _run(inv_env, name="P-0001")
    second = await run_investigation(paths=inv_env["paths"], config=inv_env["config"], target_git=inv_env["target_git"], name="P-0001",
                                     spec=DISCOVERY_SPEC, prompt="again", brief_text="BRIEF", tier=inv_env["tier"], db=inv_env["db"],
                                     resume="s1")
    assert seen == [None, "s1"] and second.attempt_id != first.attempt_id


@pytest.mark.asyncio
async def test_concurrent_branch_creation_still_discards_valid_review(monkeypatch, inv_env):
    from ateam.roles import REVIEWER_SPEC

    async def fake_query(prompt, options):
        inv_env["target_git"].run("branch", "other-session")
        yield _result({"verdict": "defensible", "findings": [], "notes": "valid review"})

    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    result = await _run(inv_env, name="P-0001-review", spec=REVIEWER_SPEC)
    assert result.res.report is None
    assert any("refs changed" in reason for reason in result.tampered)
    assert inv_env["db"].telemetry_rows()[-1]["outcome"] == "tamper"


@pytest.mark.asyncio
async def test_investigation_runs_in_the_given_checkout(monkeypatch, inv_env, target_repo, tmp_path):
    from ateam.runner import investigate as inv
    wt = tmp_path / "wt"; GitRepo(target_repo).worktree_add(wt, "wt", "main")
    seen = {}
    real_policy = inv.Policy

    def spy_policy(**kw):
        seen["policy_worktree"] = kw["worktree"]; seen["scratch"] = kw["scratch_dir"]
        return real_policy(**kw)

    async def fake_query(prompt, options):
        seen["cwd"] = options.cwd
        yield _result({"candidates": [], "unknowns": [], "notes": "n"})

    monkeypatch.setattr(inv, "Policy", spy_policy)
    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_investigation(paths=inv_env["paths"], config=inv_env["config"], target_git=GitRepo(wt), name="C-0001",
                                scratch_name="C-0001-investigate", spec=DISCOVERY_SPEC, prompt="go", brief_text="B",
                                tier=inv_env["tier"], db=inv_env["db"])
    assert r.tampered == [] and seen["cwd"] == str(wt) and seen["policy_worktree"] == wt
    assert seen["scratch"] == inv_env["paths"].scratch_root / "C-0001-investigate"
    assert inv_env["db"].telemetry_rows()[-1]["contract_id"] == "C-0001"


@pytest.mark.asyncio
async def test_investigation_in_a_worktree_sees_a_shared_ref_change_as_tamper(monkeypatch, inv_env, target_repo, tmp_path):
    wt = tmp_path / "wt"; GitRepo(target_repo).worktree_add(wt, "wt", "main")

    async def owner_branches(prompt, options):
        GitRepo(target_repo).run("branch", "owner-work")        # the owner, in their own checkout, mid-session
        yield _result({"candidates": [], "unknowns": [], "notes": "n"})

    monkeypatch.setattr("ateam.runner.run.query", owner_branches)
    r = await run_investigation(paths=inv_env["paths"], config=inv_env["config"], target_git=GitRepo(wt), name="C-0001",
                                spec=DISCOVERY_SPEC, prompt="go", brief_text="B", tier=inv_env["tier"], db=inv_env["db"])
    assert any("refs changed" in t for t in r.tampered) and r.res.report is None
