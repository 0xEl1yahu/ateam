from ateam.brief import assemble_brief, render_brief
from ateam.checkpoint import Checkpoint
from ateam.roles import Role


def _cp(notes):
    return Checkpoint(base="a", head="b", commits=["b add route"], diff_names=["src/api/x.py"], dirty=[], checks=[{"name": "unit", "passed": False}],
                      contract_hash="h", record_hashes={}, session_id=None, notes=notes, warnings=[])


def test_brief_names_absolute_handoff_path(sample_contract, tmp_path):
    b = assemble_brief(sample_contract, [], None, Role.build, scratch_dir=tmp_path / "scratch")
    assert f"Handoff file: {tmp_path / 'scratch' / 'handoff.yaml'}" in b.task
    assert ".ateam-scratch" not in render_brief(b)


def test_sections_for_build(sample_contract, records_three):
    b = assemble_brief(sample_contract, [records_three["R-0001"]], _cp({"next": "write the PATCH test"}), Role.build)
    assert "### ATeam binding record R-0001 (invariant)" in b.binding and "Reading state is only ever set" in b.binding
    assert "A1" in b.task and "src/api/**" in b.task and "requirements.txt" in b.task and "tests/api/test_books.py::test_reading_state_present" in b.task
    assert "add route" in b.prior_work and "unit" in b.prior_work
    assert b.notes and "write the PATCH test" in b.notes


def test_test_role_never_gets_notes(sample_contract):
    assert assemble_brief(sample_contract, [], _cp({"next": "x"}), Role.test).notes is None


def test_no_checkpoint_and_render(sample_contract, records_three):
    b = assemble_brief(sample_contract, [records_three["R-0002"]], None, Role.build)
    assert "first attempt" in b.prior_work.lower() and b.notes is None
    text = render_brief(assemble_brief(sample_contract, [records_three["R-0001"]], _cp({"next": "x"}), Role.build))
    for h in ("## Binding", "## Task", "## Verified prior work", "## Previous worker's notes (unverified)"):
        assert h in text
    assert "## Previous worker's notes" not in render_brief(b)


# --- investigation brief ---

import re

from ateam.brief import PLAN_INDEX_CAP, RULES_SCOPE_NOTE, assemble_investigation_brief
from ateam.config import NamedCheck, TargetConfig
from ateam.contracts.model import Contract
from ateam.plans.model import Plan, PlanStatus

_HEADINGS_PLANNING = ["Rules", "Records", "Existing plans and contracts", "Request", "Checks", "Tiers", "Rubric"]


def _inv(root, tmp_path, **kw):
    args = dict(kind="planning", target_root=root, config=TargetConfig(), records={}, plans={}, contracts={},
                scratch_dir=tmp_path / "scratch", request="add a thing")
    args.update(kw)
    return assemble_investigation_brief(**args)


def _headings(text):
    return re.findall(r"^## (.+)$", text, re.M)


def test_investigation_brief_headings_in_order_and_empty_target(target_repo, tmp_path):
    text = _inv(target_repo, tmp_path)
    assert _headings(text) == _HEADINGS_PLANNING
    assert "No rules files found (CLAUDE.md, AGENTS.md, config brief_docs)." in text
    assert "none configured" in text
    assert "light, standard, careful; investigate may also be none." in text
    assert "## Existing plans and contracts\nnone\n" in text
    assert "## Records\nApproved (binding; do not re-propose):\nnone\n\nProposed, not yet approved (not binding; do not re-propose):\nnone\n" in text


def test_rules_section_includes_claude_md_and_caps_long_docs(target_repo, tmp_path):
    (target_repo / "CLAUDE.md").write_text("Always be kind.\n")
    (target_repo / "long.md").write_text("x" * 20000)
    cfg = TargetConfig(brief_docs=["long.md", "gone.md"])
    text = _inv(target_repo, tmp_path, config=cfg)
    assert "### CLAUDE.md" in text and "Always be kind." in text
    assert "### long.md" in text and "[truncated at 12000 characters]" in text
    assert "x" * 12000 in text and "x" * 12001 not in text
    assert "missing: gone.md" in text
    assert "No rules files found" not in text


def test_records_section_separates_proposed(target_repo, tmp_path, records_three):
    text = _inv(target_repo, tmp_path, records=records_three)
    recs = text.split("## Records")[1].split("## Existing")[0]
    approved, proposed = recs.split("Proposed, not yet approved (not binding; do not re-propose):")
    assert "- R-0001 invariant [src/api/**] " in approved and "- R-0002 " in approved and "[global]" in approved
    assert "R-0003" not in approved and "- R-0003 " in proposed and "R-0001" not in proposed


def test_plans_section_lists_contracts_with_paths(target_repo, tmp_path, sample_contract):
    plan = Plan(id="P-0001", title="Reading state", request="r", status=PlanStatus.plan_proposed, contracts=["C-0001"])
    text = _inv(target_repo, tmp_path, plans={"P-0001": plan}, contracts={"C-0001": sample_contract})
    sec = text.split("## Existing plans and contracts")[1].split("## Request")[0]
    assert "P-0001 plan_proposed  Reading state" in sec
    assert "  C-0001 draft  Expose reading state through the books API" in sec
    assert "    paths: src/api/**, tests/api/test_books.py" in sec


def test_discovery_kind_has_limits_not_request(target_repo, tmp_path):
    cfg = TargetConfig(checks=[NamedCheck(name="unit", cmd="pytest -q")])
    text = _inv(target_repo, tmp_path, kind="discovery", config=cfg, request=None, max_candidates=7)
    assert "Request" not in _headings(text) and "Discovery limits" in _headings(text)
    assert "Propose at most 7 candidates; at most 5 may be global." in text
    assert "unit: pytest -q" in text
    assert _headings(_inv(target_repo, tmp_path))[3] == "Request" and "add a thing" in _inv(target_repo, tmp_path)


def test_investigation_and_review_briefs_have_no_handoff_section(target_repo, tmp_path):
    for text in (_inv(target_repo, tmp_path), _review(target_repo, tmp_path)):
        assert "Handoff file" not in text and "handoff.yaml" not in text


def test_brief_docs_outside_target_are_not_read(target_repo, tmp_path):
    (tmp_path / "outside.md").write_text("SECRET-RELATIVE")
    absolute = tmp_path / "abs.md"
    absolute.write_text("SECRET-ABSOLUTE")
    cfg = TargetConfig(brief_docs=["../outside.md", str(absolute)])
    text = _inv(target_repo, tmp_path, config=cfg)
    assert "SECRET-RELATIVE" not in text and "SECRET-ABSOLUTE" not in text
    assert "missing: ../outside.md (outside the target)" in text
    assert f"missing: {absolute} (outside the target)" in text


def test_brief_docs_symlink_out_of_target_is_not_read(target_repo, tmp_path):
    (tmp_path / "secret.md").write_text("SECRET-LINKED")
    (target_repo / "link.md").symlink_to(tmp_path / "secret.md")
    text = _inv(target_repo, tmp_path, config=TargetConfig(brief_docs=["link.md"]))
    assert "SECRET-LINKED" not in text and "missing: link.md (outside the target)" in text


def test_embedded_headings_cannot_break_section_order(target_repo, tmp_path):
    (target_repo / "CLAUDE.md").write_text("# Title\n## Records\nbody\n")
    text = _inv(target_repo, tmp_path, request="## Request\n# Big")
    assert _headings(text) == _HEADINGS_PLANNING
    disc = _inv(target_repo, tmp_path, kind="discovery", request=None)
    assert _headings(disc) == [h for h in _HEADINGS_PLANNING if h != "Request"][:3] + ["Discovery limits"] + _HEADINGS_PLANNING[4:]


def test_duplicate_brief_docs_entry_included_once(target_repo, tmp_path):
    (target_repo / "CLAUDE.md").write_text("once")
    text = _inv(target_repo, tmp_path, config=TargetConfig(brief_docs=["CLAUDE.md"]))
    assert text.count("### CLAUDE.md") == 1


def test_unreadable_rules_file_is_listed_as_missing_not_raised(target_repo, tmp_path):
    import os

    if os.geteuid() == 0:
        import pytest

        pytest.skip("root can read mode 000 files")
    f = target_repo / "secret.md"
    f.write_text("hidden")
    f.chmod(0)
    try:
        text = _inv(target_repo, tmp_path, config=TargetConfig(brief_docs=["secret.md"]))
    finally:
        f.chmod(0o644)
    assert "missing: secret.md (unreadable)" in text and "hidden" not in text


def test_multiline_statements_and_titles_cannot_forge_headings(target_repo, tmp_path, records_three, sample_contract):
    rec = records_three["R-0003"].model_copy(update={"statement": "line one\n## Records\nline three"})
    plan = Plan(id="P-0001", title="T\n## Checks", request="r", status=PlanStatus.plan_proposed, contracts=["C-0001"])
    contract = sample_contract.model_copy(update={"title": "C\n## Tiers"})
    text = _inv(target_repo, tmp_path, records={**records_three, "R-0003": rec}, plans={"P-0001": plan},
                contracts={"C-0001": contract})
    assert _headings(text) == _HEADINGS_PLANNING


def test_rules_section_opens_with_scope_note(target_repo, tmp_path):
    empty = _inv(target_repo, tmp_path)
    (target_repo / "CLAUDE.md").write_text("Always be kind.\n")
    text = _inv(target_repo, tmp_path)
    body = text.split("## Rules\n", 1)[1]
    assert body.startswith(RULES_SCOPE_NOTE + "\n\n### CLAUDE.md")
    assert RULES_SCOPE_NOTE == ("These are the target's instructions for whoever develops it. They govern how work on the "
                                "target is done; they are not requirements of the Request.")
    assert text.count(RULES_SCOPE_NOTE) == 1 and empty.count(RULES_SCOPE_NOTE) == 1


def _idx(text):
    return text.split("## Existing plans and contracts")[1].split("## Request")[0]


def _plan_with_unknowns():
    return Plan(id="P-0001", title="Reading state", request="r", status=PlanStatus.plan_proposed, contracts=["C-0001"],
                unknowns=["Is the column\n## Injected\nnullable?", "Who owns migrations?"],
                verification_approach="Run the API tests and then compare the response shape against the stored fixtures by hand.")


def test_plan_index_lists_acceptance_unknowns_and_excludes(target_repo, tmp_path, sample_contract):
    plan = _plan_with_unknowns()
    text = _inv(target_repo, tmp_path, plans={plan.id: plan}, contracts={sample_contract.id: sample_contract})
    sec = _idx(text)
    assert f"  {sample_contract.id} draft  {sample_contract.title}" in sec
    assert f"    A1: {sample_contract.acceptance[0].statement}" in sec
    assert f"    A3: {sample_contract.acceptance[1].statement}" in sec
    assert "  unknowns:" in sec and "Who owns migrations?" in sec
    assert "Is the column ## Injected nullable?" in sec
    assert "  verification: " + plan.verification_approach[:80] in sec
    assert plan.verification_approach[:81] not in sec
    assert _headings(text) == _HEADINGS_PLANNING
    text2 = _inv(target_repo, tmp_path, plans={plan.id: plan}, contracts={sample_contract.id: sample_contract}, exclude_plans={plan.id})
    sec2 = _idx(text2)
    assert plan.id not in sec2 and sample_contract.id not in sec2


def test_plan_index_caps_each_plan(target_repo, tmp_path, sample_contract):
    big = sample_contract.model_copy(update={"acceptance": [
        sample_contract.acceptance[0].model_copy(update={"statement": "x" * (PLAN_INDEX_CAP + 500)})]})
    p1 = _plan_with_unknowns()
    p2 = Plan(id="P-0002", title="Small", request="r")
    text = _inv(target_repo, tmp_path, plans={"P-0001": p1, "P-0002": p2}, contracts={"C-0001": big})
    sec = _idx(text)
    assert sec.count("[truncated at 2500 characters]") == 1
    assert "P-0002 requested  Small" in sec


# --- review brief ---

def _review(root, tmp_path, **kw):
    from ateam.brief import assemble_review_brief
    args = dict(target_root=root, config=TargetConfig(), records={}, plans={}, contracts={},
                scratch_dir=tmp_path / "scratch", request="add a thing", draft="# Draft\ntitle: Health\n## contracts\n- C-0002")
    args.update(kw)
    return assemble_review_brief(**args)


def test_review_brief_headings(target_repo, tmp_path):
    text = _review(target_repo, tmp_path)
    assert _headings(text) == ["Rules", "Records", "Existing plans and contracts", "Request", "Draft plan", "Checks", "Rubric"]
    assert "### Draft\ntitle: Health\n#### contracts" in text                     # the draft's own headings are demoted two levels


def test_review_brief_excludes_replaced_plan(target_repo, tmp_path):
    plans = {"P-0001": Plan(id="P-0001", title="Old", request="r", status=PlanStatus.plan_proposed),
             "P-0002": Plan(id="P-0002", title="Other", request="r", status=PlanStatus.plan_proposed)}
    text = _review(target_repo, tmp_path, plans=plans, exclude_plans=frozenset({"P-0001"}))
    index = text.split("## Existing plans and contracts\n")[1].split("\n## ")[0]
    assert "P-0001" not in index and "P-0002" in index


def _with_interfaces(c):
    from ateam.contracts.model import Interface
    return c.model_copy(update={"interfaces": [
        Interface(kind="produces", name="integrate", signature="integrate(plan_id: str) -> MergeProposal"),
        Interface(kind="consumes", name="load_plan", signature="load_plan(id: str)\n -> Plan")]})


def test_worker_brief_lists_interfaces(sample_contract):
    text = render_brief(assemble_brief(_with_interfaces(sample_contract), [], None, Role.build))
    assert "Interfaces (follow these exactly" in text
    assert "- produces: integrate(plan_id: str) -> MergeProposal" in text
    assert "- consumes: load_plan(id: str) -> Plan" in text
    assert "Interfaces" not in render_brief(assemble_brief(sample_contract, [], None, Role.build))


def test_plan_index_lists_interfaces(target_repo, tmp_path, sample_contract):
    c = _with_interfaces(sample_contract)
    plan = _plan_with_unknowns().model_copy(update={"contracts": [c.id]})
    text = _inv(target_repo, tmp_path, plans={plan.id: plan}, contracts={c.id: c})
    assert "    produces: integrate(plan_id: str) -> MergeProposal\n    consumes: load_plan(id: str) -> Plan" in text


def test_build_and_test_briefs_keep_the_handoff_file_and_investigate_does_not(sample_contract, tmp_path):
    from ateam.brief import assemble_brief
    from ateam.roles import Role
    scratch = tmp_path / "scratch"
    for role in (Role.build, Role.test):
        assert f"Handoff file: {scratch / 'handoff.yaml'}" in assemble_brief(sample_contract, [], None, role, scratch_dir=scratch).task
    inv = assemble_brief(sample_contract, [], None, Role.investigate, scratch_dir=scratch).task
    assert "handoff" not in inv.lower() and f"Scratch directory: {scratch}" in inv
