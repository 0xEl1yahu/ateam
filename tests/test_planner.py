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



def test_rubric_floors_applied_with_notes(target_repo, target_paths, records_three):
    rep = report(contract("A", complexity="high", risk="high", tier={"build": "light", "test": "light", "investigate": "none"}))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    t = r.contracts[0].allocation.tier
    assert t["build"] == "careful" and t["test"] == "careful"
    assert any("build tier raised" in n for n in r.problems) and any("test tier raised" in n for n in r.problems)


def test_self_dependency_message_and_duplicate_deps_deduped(target_repo, target_paths, records_three):
    with pytest.raises(PlanError) as e:
        report_to_plan(report(contract("A", deps=[0])), "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert any("itself" in p for p in e.value.problems)
    r = report_to_plan(report(contract("A"), contract("B", deps=[0, 0])), "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r.contracts[1].depends_on == ["C-0001"]


def test_missing_budget_derived_from_build_tier_and_given_budget_kept(target_repo, target_paths, records_three):
    from ateam.allocation import budget_for

    tiers = load_tiers()
    given = {"attempts": 5, "usd_per_attempt": 1.5, "max_turns": 7}
    careful = dict(complexity="high", risk="high", tier={"build": "careful", "test": "careful", "investigate": "none"})
    kept = {**contract("Kept"), "budget": given}
    rep = report(contract("Light"), contract("Careful", **careful), kept)
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records, tiers=tiers)
    assert r.contracts[0].budget == budget_for("light", tiers)
    assert r.contracts[1].budget == budget_for("careful", tiers)
    assert r.contracts[2].budget.model_dump() == given


def test_depends_on_plans_unknown_is_note_and_self_dropped(target_repo, target_paths, records_three):
    from ateam.plans.model import Plan

    rep = report(contract("A"))
    rep.update(depends_on_plans=["P-0001", "P-0002", "P-0009"], files_read=["a.py"], out_of_scope=["ui"], verification_approach="run unit")
    existing = {"P-0002": Plan(id="P-0002", title="t", request="r")}
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records, existing_plans=existing)
    assert r.plan.depends_on_plans == ["P-0002", "P-0009"]
    assert any("depends_on_plans names unknown plan P-0009" in n for n in r.problems)
    assert not any("unknown plan P-0002" in n for n in r.problems)
    assert any("P-0001" in n and "own id" in n for n in r.problems)
    assert r.plan.files_read == ["a.py"] and r.plan.out_of_scope == ["ui"] and r.plan.verification_approach == "run unit"


def test_old_report_without_new_fields_still_converts(target_repo, target_paths, records_three):
    r = report_to_plan(report(contract("A")), "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r.plan.depends_on_plans == [] and r.plan.files_read == [] and r.plan.verification_approach == ""


def test_review_prints_new_sections(target_repo, target_paths, records_three):
    rep = report(contract("A"))
    rep.update(depends_on_plans=["P-0002"], files_read=[f"f{i}.py" for i in range(25)], out_of_scope=["the UI"], verification_approach="run the unit suite")
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    text = render_plan_review(r, load_tiers())
    assert "Depends on plans: P-0002" in text
    assert "Files read (25):" in text and "f19.py" in text and "f20.py" not in text
    assert "Out of scope:" in text and "the UI" in text
    assert "Verification approach: run the unit suite" in text
    assert text.index("C-0001") < text.index("Depends on plans:")
