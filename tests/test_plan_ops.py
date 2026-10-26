from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir
from ateam.plans.model import Plan, PlanStatus
from ateam.plans.store import load_plans, save_plan
from ateam.state.db import StateDB


def _db(root):
    db = StateDB(home_dir(root) / "state.db"); db.init_schema()
    return db


def test_set_plan_status_commits_the_file_then_walks_the_store(target_repo):
    from ateam.intent import commit_intent
    from ateam.plans.ops import set_plan_status
    paths = TargetPaths.from_root(target_repo)
    db = _db(target_repo)
    for to in ("requested", "investigating", "plan_proposed", "plan_approved"):
        db.transition("feature", "P-0001", to, by="t")
    save_plan(paths.plans, Plan(id="P-0001", title="t", request="r", status=PlanStatus.plan_approved))
    commit_intent(paths, "seed", ["plans"])
    extra = paths.reports / "x.md"; extra.parent.mkdir(parents=True, exist_ok=True); extra.write_text("x\n")
    plan = load_plans(paths.plans)["P-0001"]
    out = set_plan_status(paths, db, plan, ["executing", "integrating"], by="integrate", why="w", extra_files=[extra])
    assert out.status == "integrating" and [h.to for h in out.history[-2:]] == ["executing", "integrating"]
    assert load_plans(paths.plans)["P-0001"].status == "integrating" and db.feature_status("P-0001") == "integrating"
    g = GitRepo(paths.intent)
    assert g.is_clean() and g.run("log", "-1", "--format=%s").strip() == "ateam: P-0001 -> integrating"
    assert "reports/x.md" in g.run("show", "--name-only", "--format=", "HEAD")


def test_dependency_order_puts_dependencies_first():
    from types import SimpleNamespace
    from ateam.plans.ops import dependency_order
    cs = {"C-3": SimpleNamespace(depends_on=["C-2"]), "C-2": SimpleNamespace(depends_on=[]), "C-1": SimpleNamespace(depends_on=["C-3"])}
    assert dependency_order(cs) == ["C-2", "C-3", "C-1"]
