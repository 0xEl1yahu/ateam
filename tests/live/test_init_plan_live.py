"""Opt-in: real discovery and planning sessions on the haiku alias. Set ATEAM_LIVE_TESTS=1. A few cents."""

import os

import pytest
from click.testing import CliRunner

from ateam.cli import main
from ateam.contracts.store import load_contracts
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths
from ateam.plans.store import load_plans
from ateam.records.store import load_records

pytestmark = pytest.mark.skipif(not os.environ.get("ATEAM_LIVE_TESTS"), reason="set ATEAM_LIVE_TESTS=1")


def test_init_then_plan_live(cli_target):
    (cli_target / "README.md").write_text(
        "# Rules\n\nRoute handlers under src/api never read environment variables directly; "
        "configuration arrives through function arguments.\n"
    )
    g = GitRepo(cli_target); g.run("add", "README.md"); g.run("commit", "-q", "-m", "rules")
    paths = TargetPaths.from_root(cli_target)

    r = CliRunner().invoke(main, ["init", "--tier", "light", "--max-candidates", "4", "--target", str(cli_target)])
    print("\n[init]", r.output[-1500:])
    assert r.exit_code == 0, r.output
    recs = load_records(paths.records)
    assert any(any("README" in e.ref for e in rec.evidence) for rec in recs.values()), "no record cites the README"
    assert g.is_clean()

    r = CliRunner().invoke(main, ["plan", "add a health() function in src/api that returns ok, with a unit test", "--tier", "light", "--target", str(cli_target)])
    print("\n[plan]", r.output[-2500:])
    assert r.exit_code == 0, r.output
    plans = load_plans(paths.plans)
    assert plans and plans["P-0001"].status == "plan_proposed" and plans["P-0001"].contracts
    cs = load_contracts(paths.contracts)
    c = cs[plans["P-0001"].contracts[0]]
    assert any(p.startswith("src") for p in c.paths) and c.allocation.complexity in ("low", "medium", "high")
    assert g.is_clean()
