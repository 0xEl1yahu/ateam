"""Where ATeam keeps things: durable intent in the target, operational state at home."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TargetPaths:
    """Locations inside a target repository."""

    root: Path
    ateam: Path
    records: Path
    contracts: Path
    plans: Path
    reports: Path
    config_file: Path

    @classmethod
    def from_root(cls, root: Path) -> TargetPaths:
        ateam = root / ".ateam"
        return cls(
            root=root,
            ateam=ateam,
            records=ateam / "records",
            contracts=ateam / "contracts",
            plans=ateam / "plans",
            reports=ateam / "reports",
            config_file=ateam / "config.yaml",
        )

    def exists(self) -> bool:
        return self.ateam.is_dir()


def repo_id(root: Path) -> str:
    """Stable short identifier for a target repo, derived from its resolved path."""
    return hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]


def home_dir(root: Path) -> Path:
    """Operational state directory for a target: $ATEAM_HOME/<id> or ~/.ateam/<id>."""
    base = Path(os.environ.get("ATEAM_HOME") or (Path.home() / ".ateam"))
    path = base / repo_id(root)
    path.mkdir(parents=True, exist_ok=True)
    return path
