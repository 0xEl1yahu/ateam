"""The record schema. One kind of record with a `kind` field."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

RECORD_ID_RE = re.compile(r"^R-\d{4}$")

EvidenceType = Literal["doc", "claude-md", "agents-md", "commit-message", "code", "test", "commit"]
DOCUMENTED_EVIDENCE: frozenset[str] = frozenset({"doc", "claude-md", "agents-md", "commit-message"})


class Kind(StrEnum):
    decision = "decision"
    invariant = "invariant"
    convention = "convention"


class Status(StrEnum):
    proposed = "proposed"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"
    retired = "retired"


class Origin(StrEnum):
    documented = "documented"
    inferred = "inferred"


class Basis(StrEnum):
    requirement = "requirement"
    observation = "observation"


class Evidence(BaseModel):
    type: EvidenceType
    ref: str
    note: str | None = None


class Binds(BaseModel):
    paths: list[str] = Field(default_factory=list)
    incidental: list[str] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    at: datetime
    by: str
    to: Status
    why: str | None = None


class Record(BaseModel):
    id: str = Field(pattern=RECORD_ID_RE.pattern)
    kind: Kind
    status: Status
    origin: Origin
    basis: Basis
    statement: str
    rationale: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    binds: Binds = Field(default_factory=Binds)
    supersedes: list[str] = Field(default_factory=list)
    serves: list[str] = Field(default_factory=list)
    fingerprint: str
    history: list[HistoryEntry] = Field(default_factory=list)

    def is_global(self) -> bool:
        return not self.binds.paths

    def content_hash(self) -> str:
        """Hash of everything except history, so approvals do not change it."""
        payload = self.model_dump_json(exclude={"history"})
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()

    def slug(self) -> str:
        words = re.sub(r"[^a-z0-9]+", "-", self.statement.lower()).strip("-").split("-")
        return "-".join(w for w in words[:6] if w)[:40].rstrip("-")


def compute_fingerprint(evidence: list[Evidence]) -> str:
    """Identity of a proposal by its evidence, so rejected ones are not re-proposed."""
    keys = sorted(f"{e.type}:{e.ref}" for e in evidence)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _canonical(json_text: str) -> str:
    import json

    return json.dumps(json.loads(json_text), sort_keys=True, separators=(",", ":"))
