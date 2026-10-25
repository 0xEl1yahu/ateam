import pytest

from ateam.config import NamedCheck, TargetConfig
from ateam.contracts.model import ContractStatus
from ateam.planner import PlanError, PlanResult, render_plan_review, report_to_plan
from ateam.tiers import load_tiers

CFG = TargetConfig(checks=[NamedCheck(name="unit", cmd="python3 -m unittest discover -s tests -t . -q")])


def contract(title="Add health", paths=("src/api/**", "tests/**"), deps=(), checks=("unit",), **alloc):
    a = dict(complexity="low", risk="low", uncertainty="low", reasoning="one function; no boundaries", tier={"build": "light", "test": "light", "investigate": "none"})
    a.update(alloc)
    return {
        "title": title, "paths": list(paths), "incidental": [], "depends_on": list(deps),
        "acceptance": [{"id": "A1", "statement": "health() returns ok", "check": {"type": "test", "ref": "tests/test_routes.py::RoutesTest::test_health"}}],
        "checks": list(checks), "allocation": a,
    }


def report(*contracts, proposed=(), unknowns=(), title="Health endpoint"):
    return {"title": title, "contracts": list(contracts), "proposed_records": list(proposed), "unknowns": list(unknowns), "notes": "n"}


def test_report_to_plan_happy_path_ids_deps_tiers(target_repo, target_paths, records_three):
    rep = report(contract("First"), contract("Second", deps=[0], complexity="high", risk="medium", tier={"build": "careful", "test": "careful", "investigate": "none"}))
    r = report_to_plan(rep, "add health", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert isinstance(r, PlanResult) and r.plan.id == "P-0001" and r.plan.title == "Health endpoint" and r.plan.request == "add health"
    assert [c.id for c in r.contracts] == ["C-0001", "C-0002"] and r.plan.contracts == ["C-0001", "C-0002"]
    assert r.contracts[1].depends_on == ["C-0001"] and all(c.status == ContractStatus.draft and c.plan == "P-0001" for c in r.contracts)
    assert r.contracts[0].checks[0].cmd.startswith("python3 -m unittest")
    assert r.bound_preview["C-0001"] == ["R-0001", "R-0002"]   # src/api/** binds R-0001; R-0002 is global
    assert r.problems == []


def test_dependency_cycle_refused_nothing_written(target_repo, target_paths, records_three):
    rep = report(contract("A", deps=[1]), contract("B", deps=[0]))
    with pytest.raises(PlanError) as e:
        report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert any("cycle" in p for p in e.value.problems)
    assert list(target_paths.contracts.glob("C-*.yaml")) == [] and list(target_paths.records.glob("R-*.yaml")) == []


def test_out_of_range_dependency_and_unknown_check_refused(target_repo, target_paths, records_three):
    with pytest.raises(PlanError) as e:
        report_to_plan(report(contract("A", deps=[3])), "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert any("depends_on" in p and "A" in p for p in e.value.problems)
    with pytest.raises(PlanError) as e2:
        report_to_plan(report(contract("B", checks=["lint"])), "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert any("lint" in p and "B" in p for p in e2.value.problems)


def test_invalid_glob_refused(target_repo, target_paths, records_three):
    with pytest.raises(PlanError) as e:
        report_to_plan(report(contract("A", paths=("./src/**",))), "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert any("glob" in p for p in e.value.problems)


def test_test_tier_raised_to_build_noted(target_repo, target_paths, records_three):
    rep = report(contract("A", complexity="high", tier={"build": "careful", "test": "light", "investigate": "none"}))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r.contracts[0].allocation.tier["test"] == "careful" and any("test" in p and "careful" in p for p in r.problems)


def test_investigate_tier_set_on_high_uncertainty(target_repo, target_paths, records_three):
    rep = report(contract("A", uncertainty="high"))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r.contracts[0].allocation.tier["investigate"] == "standard" and any("investigate" in p for p in r.problems)


def test_acceptance_ref_outside_paths_is_a_warning_not_fatal(target_repo, target_paths, records_three):
    rep = report(contract("A", paths=("src/api/**",)))   # test ref lives in tests/, not in paths
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert any("tests/test_routes.py" in p for p in r.problems) and len(r.contracts) == 1


def test_plan_without_approved_records_ok(target_repo, target_paths):
    r = report_to_plan(report(contract("A")), "x", "P-0001", target_paths.contracts, {}, CFG, target_repo, target_paths.records)
    assert r.bound_preview["C-0001"] == []
    text = render_plan_review(r, load_tiers())
    assert "no approved records" in text.lower()


def test_proposed_records_go_through_discovery_rules(target_repo, target_paths, records_three):
    cand = {"kind": "convention", "statement": "Health endpoints return plain strings", "rationale": "simple", "origin": "inferred", "basis": "requirement",
            "evidence": [{"type": "code", "ref": "src/api/x.py:1"}], "paths": ["src/api/**"], "serves": [], "confidence": "medium"}
    r = report_to_plan(report(contract("A"), proposed=[cand]), "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r.proposed.proposed[0].basis == "observation" and r.plan.proposed_records == [r.proposed.proposed[0].id]


def test_render_review_contains_allocation_and_command(target_repo, target_paths, records_three):
    r = report_to_plan(report(contract("A"), unknowns=["which port"]), "add health", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    text = render_plan_review(r, load_tiers())
    for needle in ("P-0001", "C-0001", "complexity: low", "risk: low", "uncertainty: low", "haiku", "A1", "unit", "R-0001", "which port", "ateam approve P-0001", "--tier C-0001=careful"):
        assert needle in text, needle
