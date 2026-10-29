"""The contract schema."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ateam.config import NamedCheck

CONTRACT_ID_RE = re.compile(r"^C-\d{4}$")

__all__ = [
    "Acceptance", "Allocation", "Budget", "Check", "Contract", "ContractStatus", "Interface", "Level", "NamedCheck", "RecordRef",
]


class ContractStatus(StrEnum):
    draft = "draft"
    ready = "ready"
    building = "building"
    verifying = "verifying"
    verified = "verified"
    failed = "failed"
    paused = "paused"
    integrated = "integrated"
    superseded = "superseded"


class Level(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Check(BaseModel):
    type: Literal["test", "command", "judgment"]
    ref: str | None = None


class Acceptance(BaseModel):
    id: str
    statement: str
    check: Check


HEADER_STARTS = ("def ", "async def ", "class ")


def normalise_header(text: str) -> str | None:
    """Normalise a def/async def/class header line for comparison; None if the text is not a header."""
    line = " ".join(text.split())
    if not line.startswith(HEADER_STARTS) or not line.endswith(":"):
        return None
    line = re.sub(r"([(\[{]) ", r"\1", line)
    line = re.sub(r" ([)\]}])", r"\1", line)
    return re.sub(r",([)\]}])", r"\1", line)   # black's magic trailing comma


class Interface(BaseModel):
    kind: Literal["produces", "consumes"]
    name: str
    signature: str
    note: str = ""

    def code_header(self) -> str | None:
        return normalise_header(self.signature)


class Allocation(BaseModel):
    complexity: Level
    risk: Level
    uncertainty: Level
    reasoning: str
    tier: dict[str, str]


class Budget(BaseModel):
    attempts: int = 3
    usd_per_attempt: float = 2.0
    max_turns: int = 60


class RecordRef(BaseModel):
    id: str
    hash: str


class Contract(BaseModel):
    id: str = Field(pattern=CONTRACT_ID_RE.pattern)
    plan: str
    title: str
    status: ContractStatus = ContractStatus.draft
    depends_on: list[str] = Field(default_factory=list)
    paths: list[str]
    incidental: list[str] = Field(default_factory=list)
    records: list[RecordRef] = Field(default_factory=list)
    acceptance: list[Acceptance]
    interfaces: list[Interface] = Field(default_factory=list)
    checks: list[NamedCheck] = Field(default_factory=list)
    allocation: Allocation
    budget: Budget = Field(default_factory=Budget)

    def content_hash(self) -> str:
        # interfaces are binding; an empty list is left out so contracts frozen before the field existed keep their hash
        data = json.loads(self.model_dump_json(exclude={"status"} | (set() if self.interfaces else {"interfaces"})))
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def slug(self) -> str:
        words = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-").split("-")
        return "-".join(w for w in words[:6] if w)[:40].rstrip("-")
