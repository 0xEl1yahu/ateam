"""Plans on disk: one YAML file per plan under the intent repo's plans/."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from ateam.plans.model import Plan
from ateam.records.store import _FILE_RE, _summarise
from ateam.records.store import next_id as _next_id


class PlanStoreError(Exception):
    pass


def load_plans(plans_dir: Path) -> dict[str, Plan]:
    plans: dict[str, Plan] = {}
    sources: dict[str, Path] = {}
    for path in sorted(plans_dir.glob("*.y*ml")) if plans_dir.is_dir() else []:
        m = _FILE_RE.match(path.name)
        try:
            plan = Plan.model_validate(yaml.safe_load(path.read_text()) or {})
        except (yaml.YAMLError, ValidationError) as e:
            raise PlanStoreError(f"{path.name}: {_summarise(e)}") from e
        if not m or m.group("id") != plan.id:
            raise PlanStoreError(f"{path.name}: filename does not match plan id {plan.id!r}")
        if plan.id in plans:
            raise PlanStoreError(f"duplicate plan id {plan.id}: {sources[plan.id].name} and {path.name}")
        plans[plan.id] = plan
        sources[plan.id] = path
    return plans


def save_plan(plans_dir: Path, plan: Plan) -> Path:
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan.id}-{plan.slug()}.yaml"
    for old in plans_dir.glob(f"{plan.id}-*.yaml"):
        if old != path:
            old.unlink()
    path.write_text(yaml.safe_dump(plan.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
    return path


def next_id(plans_dir: Path, prefix: str = "P") -> str:
    return _next_id(plans_dir, prefix)
