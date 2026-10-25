"""Opt-in end-to-end run against the real Claude Code CLI. Costs a few cents. Set ATEAM_LIVE_TESTS=1."""

import os

import pytest

from ateam.config import TargetConfig
from ateam.contracts.model import Acceptance, Check
from ateam.contracts.select import freeze, select_records
from ateam.contracts.store import load_contracts, save_contract
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir
from ateam.records.graph import RecordGraph
from ateam.records.service import RecordService
from ateam.records.store import load_records
from ateam.roles import Role
from ateam.runner.run import run_contract_attempt
from ateam.state.db import StateDB
from ateam.tiers import load_tiers

pytestmark = pytest.mark.skipif(not os.environ.get("ATEAM_LIVE_TESTS"), reason="set ATEAM_LIVE_TESTS=1")


@pytest.fixture
def live_target(cli_target, sample_contract, tmp_path):
    paths = TargetPaths.from_root(cli_target)
    git = GitRepo(cli_target)
    RecordService(paths, git, TargetConfig()).approve("R-0001")
    records = load_records(paths.records)
    contract = sample_contract.model_copy(update={
        "title": "Add a routes comment",
        "paths": ["src/api/**"],
        "acceptance": [Acceptance(id="A1", statement="src/api/x.py starts with a comment line '# routes for the books API'", check=Check(type="judgment"))],
        "allocation": sample_contract.allocation.model_copy(update={"tier": {"build": "light", "test": "light", "investigate": "none"}}),
    })
    frozen = freeze(contract, select_records(contract.paths, RecordGraph(records), cli_target))
    save_contract(paths.contracts, frozen)
    git.commit_paths([".ateam"], "ateam: freeze C-0001")
    db = StateDB(home_dir(cli_target) / "state.db"); db.init_schema()
    db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
    wt = tmp_path / "wt-C-0001"
    git.worktree_add(wt, "ateam/C-0001", "main")
    git.exclude_in_worktree(wt, [".ateam-scratch/"])
    (wt / ".ateam-scratch").mkdir()
    return dict(paths=paths, config=TargetConfig(), git=git, wt=wt, contract=load_contracts(paths.contracts)["C-0001"], records=records, db=db)


@pytest.mark.asyncio
async def test_build_worker_end_to_end(live_target):
    t = live_target
    wt_git = GitRepo(t["wt"])
    base = wt_git.head()
    tiers = load_tiers({"light": {"build": {"max_turns": 10, "usd": 0.5}}})
    res, tree, scope, cp = await run_contract_attempt(
        paths=t["paths"], config=t["config"], git_worktree=wt_git, worktree=t["wt"], base=base,
        contract=t["contract"], records=t["records"], role=Role.build, tiers=tiers, db=t["db"],
        prompt="Implement the contract: add the comment line as the first line of src/api/x.py, update .ateam-scratch/handoff.yaml, commit in this worktree, then report.",
    )
    print("\nsubtype", res.subtype, "turns", res.num_turns, "cost", res.cost_usd, "denials", res.denials)
    print("report", res.report)
    assert res.subtype == "success" and res.report and res.report["status"] in {"done", "blocked"}
    assert scope.ok, (scope, tree)
    assert tree.changed_vs_base == ["src/api/x.py"]
    assert t["db"].telemetry_rows()[-1]["outcome"] == "completed"
    assert cp.notes is not None
    assert res.cost_usd < 0.05
