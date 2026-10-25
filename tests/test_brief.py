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

from ateam.brief import assemble_investigation_brief
from ateam.config import NamedCheck, TargetConfig
from ateam.contracts.model import Contract
from ateam.plans.model import Plan, PlanStatus

_HEADINGS_PLANNING = ["Rules", "Records", "Existing plans and contracts", "Request", "Checks", "Tiers", "Rubric", "Handoff file"]


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
    assert "## Records\nnone\n\nProposed, not yet approved (not binding; do not re-propose):\nnone\n" in text


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
    assert "  C-0001 draft  Expose reading state through the books API  [src/api/**, tests/api/test_books.py]" in sec


def test_discovery_kind_has_limits_not_request(target_repo, tmp_path):
    cfg = TargetConfig(checks=[NamedCheck(name="unit", cmd="pytest -q")])
    text = _inv(target_repo, tmp_path, kind="discovery", config=cfg, request=None, max_candidates=7)
    assert "Request" not in _headings(text) and "Discovery limits" in _headings(text)
    assert "Propose at most 7 candidates; at most 5 may be global." in text
    assert "unit: pytest -q" in text
    assert _headings(_inv(target_repo, tmp_path))[3] == "Request" and "add a thing" in _inv(target_repo, tmp_path)


def test_handoff_line_present(target_repo, tmp_path):
    text = _inv(target_repo, tmp_path)
    assert text.rstrip().endswith(f"## Handoff file\n{tmp_path / 'scratch' / 'handoff.yaml'}")
