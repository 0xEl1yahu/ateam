"""The CLI over integrate and merge: exit codes, output, and reject of a merge_proposed plan."""

from ateam.cli import Context
from ateam.config import NamedCheck
from ateam.contracts.store import load_contracts
from ateam.gitops import GitRepo
from ateam.plans.store import load_plans
from test_integrate import _commit_on_main, _set_config, approved, run, verified  # noqa: F401  (fixtures)


def test_integrate_then_merge_end_to_end(verified):
    root = str(verified.root)
    r = run("integrate", "P-0001", "--target", root)
    assert r.exit_code == 0, r.output
    assert "C-0001 -> integrated" in r.output and "C-0002 -> integrated" in r.output and "P-0001 -> merge_proposed" in r.output
    assert "ateam merge P-0001" in r.output
    tip = GitRepo(verified.root).rev_parse("ateam/P-0001")
    r = run("merge", "P-0001", "--target", root)
    assert r.exit_code == 0, r.output
    assert f"main fast-forwarded to {tip[:12]}" in r.output and "P-0001 -> merged" in r.output
    assert GitRepo(verified.root).head() == tip and Context(verified.root).db.feature_status("P-0001") == "merged"


def test_integrate_refused_exits_4(approved):
    r = run("integrate", "P-0001", "--target", str(approved.root))
    assert r.exit_code == 4 and "error: C-0001 is ready" in r.output and "Traceback" not in r.output


def test_integrate_conflict_exits_3(verified):
    _commit_on_main(verified.root, "src/a/a.py", "A = 'main'\n")
    r = run("integrate", "P-0001", "--target", str(verified.root))
    assert r.exit_code == 3 and "C-0001 conflicts in src/a/a.py" in r.output


def test_integrate_failed_checks_exit_1(verified):
    _set_config(verified.paths, checks=[NamedCheck(name="unit", cmd="true"), NamedCheck(name="lint", cmd="false")])
    r = run("integrate", "P-0001", "--target", str(verified.root))
    assert r.exit_code == 1 and "lint: failed" in r.output and "P-0001 -> paused" in r.output


def test_merge_refused_exits_4_and_fast_forward_impossible_exits_1(verified):
    root = str(verified.root)
    r = run("merge", "P-0001", "--target", root)
    assert r.exit_code == 4 and "merge needs merge_proposed" in r.output
    assert run("integrate", "P-0001", "--target", root).exit_code == 0
    moved = _commit_on_main(verified.root, "docs/x.md", "x\n")
    r = run("merge", "P-0001", "--target", root)
    assert r.exit_code == 1 and "fast-forward is impossible" in r.output and GitRepo(verified.root).head() == moved


def test_reject_merge_proposed_plan(verified):
    root = str(verified.root)
    assert run("integrate", "P-0001", "--target", root).exit_code == 0
    owner = GitRepo(verified.root)
    tip, main = owner.rev_parse("ateam/P-0001"), owner.rev_parse("main")
    r = run("reject", "P-0001", "not this time", "--target", root)
    assert r.exit_code == 0, r.output
    assert load_plans(verified.paths.plans)["P-0001"].status == "plan_rejected"
    assert Context(verified.root).db.feature_status("P-0001") == "plan_rejected"
    assert owner.rev_parse("ateam/P-0001") == tip and owner.rev_parse("main") == main
    assert load_contracts(verified.paths.contracts)["C-0001"].status == "integrated"


def test_reject_still_refuses_an_approved_plan_that_has_started(verified):
    r = run("reject", "P-0001", "no", "--target", str(verified.root))
    assert r.exit_code == 1 and "P-0001 cannot be rejected (C-0001 has a contract transition)" in r.output
