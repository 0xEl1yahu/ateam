import pytest

from ateam.plans.model import Plan
from ateam.plans.store import PlanStoreError, load_plans, next_id, save_plan


def test_roundtrip_next_id_and_mismatch(target_paths):
    d = target_paths.plans
    assert next_id(d) == "P-0001"
    p = Plan(id="P-0001", title="Add health", request="add health", contracts=["C-0001"], unknowns=["which port"])
    path = save_plan(d, p)
    assert path.name == "P-0001-add-health.yaml" and load_plans(d)["P-0001"] == p
    assert next_id(d) == "P-0002"
    (d / "P-0009-copy.yaml").write_text(path.read_text())
    with pytest.raises(PlanStoreError, match="P-0009-copy.yaml"):
        load_plans(d)
