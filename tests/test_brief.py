from ateam.brief import assemble_brief, render_brief
from ateam.checkpoint import Checkpoint
from ateam.roles import Role


def _cp(notes):
    return Checkpoint(base="a", head="b", commits=["b add route"], diff_names=["src/api/x.py"], dirty=[], checks=[{"name": "unit", "passed": False}],
                      contract_hash="h", record_hashes={}, session_id=None, notes=notes, warnings=[])


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
