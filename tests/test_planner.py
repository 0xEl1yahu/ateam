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


def _plan(pid, status):
    from ateam.plans.model import Plan

    return Plan(id=pid, title="t", request="r", status=status)


def _empty_report(**extra):
    rep = report()
    rep.update(extra)
    return rep


def test_zero_contracts_with_live_dependency_is_covered(target_repo, target_paths, records_three):
    from ateam.plans.model import PlanStatus

    rep = _empty_report(depends_on_plans=["P-0001"], proposed_records=[], unknowns=["already done by P-0001"])
    r = report_to_plan(rep, "x", "P-0007", target_paths.contracts, records_three, CFG, target_repo, target_paths.records,
                       existing_plans={"P-0001": _plan("P-0001", PlanStatus.plan_proposed)})
    assert r.covered and r.plan.status == PlanStatus.plan_covered and r.plan.covered_by == ["P-0001"] and r.contracts == []
    assert r.plan.contracts == [] and r.proposed.proposed == []
    text = render_plan_review(r, load_tiers())
    assert "P-0007 covered by P-0001; no contracts written" in text


@pytest.mark.parametrize("status", ["plan_rejected", "plan_covered", "superseded", "abandoned"])
def test_zero_contracts_naming_a_dead_plan_is_fatal(status, target_repo, target_paths, records_three):
    from ateam.plans.model import PlanStatus

    rep = _empty_report(depends_on_plans=["P-0001"])
    with pytest.raises(PlanError, match="no contracts"):
        report_to_plan(rep, "x", "P-0007", target_paths.contracts, records_three, CFG, target_repo, target_paths.records,
                       existing_plans={"P-0001": _plan("P-0001", PlanStatus(status))})


def test_zero_contracts_naming_the_replaced_plan_is_fatal(target_repo, target_paths, records_three):
    from ateam.plans.model import PlanStatus

    rep = _empty_report(depends_on_plans=["P-0001"])
    with pytest.raises(PlanError, match="no contracts"):
        report_to_plan(rep, "x", "P-0007", target_paths.contracts, records_three, CFG, target_repo, target_paths.records,
                       existing_plans={"P-0001": _plan("P-0001", PlanStatus.plan_proposed)}, replaces="P-0001")


def test_zero_contracts_without_dependencies_or_with_unknown_one_is_fatal(target_repo, target_paths, records_three):
    for deps in ([], ["P-0009"]):
        with pytest.raises(PlanError, match="no contracts"):
            report_to_plan(_empty_report(depends_on_plans=deps), "x", "P-0007", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)


def test_unknowns_mentioning_a_plan_not_in_dependencies_adds_note(target_repo, target_paths, records_three):
    from ateam.plans.model import PlanStatus

    existing = {"P-0002": _plan("P-0002", PlanStatus.plan_proposed), "P-0003": _plan("P-0003", PlanStatus.plan_proposed)}
    rep = report(contract("A"), unknowns=["'verified' comes from P-0002", "see P-0003"])
    rep.update(depends_on_plans=["P-0003"], notes="also relies on P-0002")
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records, existing_plans=existing)
    assert "unknowns mention P-0002, which is not in depends_on_plans" in r.problems
    assert not any("P-0003, which is not" in n for n in r.problems)
    assert sum("P-0002, which is not" in n for n in r.problems) == 1


def test_plan_fields_default_so_content_hash_is_stable():
    from ateam.plans.model import Plan

    p = Plan(id="P-0001", title="t", request="r")
    assert p.covered_by == [] and p.replaces is None


def test_pending_path_record_kept_through_the_planner(target_repo, target_paths, records_three):
    cand = dict(kind="invariant", statement="Integrator owns merges", rationale="x", origin="inferred", basis="observation",
                evidence=[{"type": "doc", "ref": "README.md#x"}], paths=["src/ateam/newmod.py"], serves=[])
    rep = report(contract("A", paths=("src/ateam/newmod.py",)), proposed=[cand])
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert len(r.plan.proposed_records) == 1


def _ifc(kind, name, signature):
    return {"kind": kind, "name": name, "signature": signature, "note": ""}


def _with(c, *ifaces):
    return {**c, "interfaces": list(ifaces)}


def test_interface_signature_mismatch_is_a_note(target_repo, target_paths, records_three):
    rep = report(_with(contract("First"), _ifc("produces", "foo", "foo(a)")),
                 _with(contract("Second", deps=[0]), _ifc("consumes", "foo", "foo(a, b)")))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert "C-0002 consumes `foo(a, b)` but C-0001 produces `foo(a)`" in r.problems
    assert [i.name for i in r.contracts[0].interfaces] == ["foo"] and r.contracts[1].interfaces[0].kind == "consumes"


def test_interface_matching_producer_is_quiet(target_repo, target_paths, records_three):
    rep = report(_with(contract("First"), _ifc("produces", "foo", "foo(a)")),
                 _with(contract("Second", deps=[0]), _ifc("consumes", "foo", "foo(a)")))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r.problems == []


def test_consume_without_producer_or_text_match_is_a_note(target_repo, target_paths, records_three):
    rep = report(contract("First"), _with(contract("Second", deps=[0]), _ifc("consumes", "bar", "bar()")))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert any("bar" in n and "could not verify" in n for n in r.problems)


def test_consume_found_in_code_text_is_quiet(target_repo, target_paths, records_three):
    (target_repo / "src" / "api").mkdir(parents=True, exist_ok=True)
    (target_repo / "src" / "api" / "mod.py").write_text("def existing_helper(x):\n    return x\n")
    rep = report(_with(contract("First"), _ifc("consumes", "existing_helper", "existing_helper(x)")))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r.problems == []
    rep2 = {**report(_with(contract("A", paths=("docs/**", "tests/**")), _ifc("consumes", "existing_helper", "existing_helper(x)"))),
            "files_read": ["src/api/mod.py"]}
    r2 = report_to_plan(rep2, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert r2.problems == []


def test_plan_review_prints_interfaces(target_repo, target_paths, records_three):
    rep = report(_with(contract("First"), _ifc("produces", "foo", "foo(a) -> int")))
    r = report_to_plan(rep, "x", "P-0001", target_paths.contracts, records_three, CFG, target_repo, target_paths.records)
    assert "  interfaces:\n    produces: foo(a) -> int" in render_plan_review(r, load_tiers())


def test_planner_schema_requires_interfaces_and_spec_sentences():
    from ateam.roles import PLANNER_SCHEMA, PLANNER_SPEC, REVIEWER_SPEC

    items = PLANNER_SCHEMA["properties"]["contracts"]["items"]
    assert "interfaces" in items["required"]
    i = items["properties"]["interfaces"]["items"]
    assert i["additionalProperties"] is False and i["required"] == ["kind", "name", "signature", "note"]
    assert i["properties"]["kind"]["enum"] == ["produces", "consumes"]
    assert "must match the producing contract's signature exactly" in PLANNER_SPEC.system_prompt
    assert "every consumed interface is produced by an earlier contract" in REVIEWER_SPEC.system_prompt


@pytest.mark.parametrize("name", ["StateDB.transition", "store.StateDB.transition", "store.helper"])
def test_qualified_interface_definitions_are_found(tmp_path, name):
    from ateam.planner import _interface_notes
    from ateam.contracts.model import Interface
    (tmp_path / "store.py").write_text("class StateDB:\n    async def transition(self): pass\ndef helper(): pass\n")
    notes = _interface_notes([{"paths": ["*.py"]}], ["C-1"],
                             [[Interface(kind="consumes", name=name, signature="existing")]], tmp_path, [])
    assert notes == []


@pytest.mark.parametrize("name", ["WrongClass.transition", "absent"])
def test_unresolved_interface_does_not_claim_nonexistence(tmp_path, name):
    from ateam.planner import _interface_notes
    from ateam.contracts.model import Interface
    (tmp_path / "store.py").write_text("class StateDB:\n    def transition(self): pass\n")
    notes = _interface_notes([{"paths": ["*.py"]}], ["C-1"],
                             [[Interface(kind="consumes", name=name, signature="existing")]], tmp_path, [])
    assert len(notes) == 1 and "could not verify" in notes[0]
    assert "does not define" not in notes[0]


@pytest.mark.parametrize("name", ["GitRepo.run / _run", "preflight, reconcile, IntegrationEnv"])
def test_grouped_interfaces_request_separate_entries(tmp_path, name):
    from ateam.planner import _interface_notes
    from ateam.contracts.model import Interface
    notes = _interface_notes([{}], ["C-1"],
                             [[Interface(kind="consumes", name=name, signature="see earlier contract")]], tmp_path, [])
    assert len(notes) == 1 and "one interface per entry" in notes[0]


@pytest.mark.parametrize("name", ["reports/plan.yaml", "ateam integrate", "--target"])
def test_non_python_interfaces_remain_searchable(tmp_path, name):
    from ateam.planner import _interface_notes
    from ateam.contracts.model import Interface
    (tmp_path / "README.md").write_text(name)
    assert _interface_notes([{"paths": ["*.md"]}], ["C-1"],
                            [[Interface(kind="consumes", name=name, signature="existing")]], tmp_path, []) == []


def test_interface_search_does_not_follow_external_file_symlink(tmp_path):
    from ateam.planner import _interface_notes
    from ateam.contracts.model import Interface
    repo = tmp_path / "repo"; repo.mkdir()
    outside = tmp_path / "outside.py"; outside.write_text("def external_only(): pass\n")
    (repo / "linked.py").symlink_to(outside)
    notes = _interface_notes([{"paths": ["*.py"]}], ["C-1"],
                             [[Interface(kind="consumes", name="external_only", signature="existing")]], repo, [])
    assert len(notes) == 1 and "could not verify" in notes[0]


def test_interface_search_cap_and_invalid_python_are_inconclusive(tmp_path, monkeypatch):
    import ateam.planner as planner
    from ateam.contracts.model import Interface
    (tmp_path / "bad.py").write_text("class StateDB: ???")
    iface = Interface(kind="consumes", name="StateDB.transition", signature="existing")
    notes = planner._interface_notes([{"paths": ["*.py"]}], ["C-1"], [[iface]], tmp_path, [])
    assert len(notes) == 1 and "could not verify" in notes[0]
    (tmp_path / "bad.py").write_text("class StateDB:\n    def transition(self): pass\n")
    monkeypatch.setattr(planner, "_TEXT_SEARCH_CAP", 10)
    notes = planner._interface_notes([{"paths": ["*.py"]}], ["C-1"], [[iface]], tmp_path, [])
    assert len(notes) == 1 and "could not verify" in notes[0]
