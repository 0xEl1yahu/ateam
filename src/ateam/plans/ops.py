"""Plan operations shared by the CLI, the integrator and the merge gate."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ateam.intent import commit_intent_files
from ateam.paths import TargetPaths
from ateam.plans.model import Plan, PlanHistoryEntry, PlanStatus
from ateam.plans.store import save_plan
from ateam.state.db import StateDB


def plan_contract_ids(db: StateDB, plan: Plan) -> list[str]:
    """A plan's contracts: those its file lists and those the state store files under it."""
    return sorted(set(plan.contracts) | {r["contract_id"] for r in db.contracts_for_plan(plan.id)})


def dependency_order(contracts: dict) -> list[str]:
    """Contract ids with dependencies first; ties by id. Cycles were rejected at planning time."""
    remaining = dict(contracts)
    order: list[str] = []
    while remaining:
        ready = sorted(cid for cid, c in remaining.items() if all(d not in remaining for d in c.depends_on))
        if not ready:
            ready = sorted(remaining)[:1]
        for cid in ready:
            order.append(cid)
            remaining.pop(cid)
    return order


def set_plan_status(
    paths: TargetPaths, db: StateDB, plan: Plan, steps: list[str], by: str, why: str | None = None,
    extra_files: list[Path] | tuple = (),
) -> Plan:
    """Walk a plan through `steps`: the plan file (with `extra_files`) in one intent commit first, then each store
    transition. A crash between the two leaves the file ahead of the store, which the integrator reconciles."""
    now = datetime.now(UTC)
    plan = plan.model_copy(update={
        "status": PlanStatus(steps[-1]),
        "history": [*plan.history, *(PlanHistoryEntry(at=now, by=by, to=PlanStatus(s), why=why) for s in steps)],
    })
    commit_intent_files(paths, f"ateam: {plan.id} -> {steps[-1]}", [save_plan(paths.plans, plan), *extra_files])
    for s in steps:
        db.transition("feature", plan.id, s, by=by, reason=why)
    return plan
