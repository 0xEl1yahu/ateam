"""The plan schema. Status values are the feature state machine in state/db.py."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

PLAN_ID_RE = re.compile(r"^P-\d{4}$")


class PlanStatus(StrEnum):
    requested = "requested"
    investigating = "investigating"
    plan_proposed = "plan_proposed"
    plan_approved = "plan_approved"
    plan_rejected = "plan_rejected"
    executing = "executing"
    paused = "paused"
    integrating = "integrating"
    merge_proposed = "merge_proposed"
    merged = "merged"
    abandoned = "abandoned"


class PlanHistoryEntry(BaseModel):
    at: datetime
    by: str
    to: PlanStatus
    why: str | None = None


class Plan(BaseModel):
    id: str = Field(pattern=PLAN_ID_RE.pattern)
    title: str
    request: str
    status: PlanStatus = PlanStatus.requested
    contracts: list[str] = Field(default_factory=list)
    proposed_records: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    depends_on_plans: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    verification_approach: str = ""
    tier_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    history: list[PlanHistoryEntry] = Field(default_factory=list)

    def slug(self) -> str:
        words = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-").split("-")
        return "-".join(w for w in words[:6] if w)[:40].rstrip("-")

    def content_hash(self) -> str:
        data = json.loads(self.model_dump_json(exclude={"status", "history"}))
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
