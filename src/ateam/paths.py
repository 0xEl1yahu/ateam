"""Where ATeam keeps things.

Durable intent (records, contracts, config) lives in a private intent repo under ATeam's
home, keyed by the target's origin URL so every clone of the target on every machine finds
the same intent. Operational state (SQLite, scratch, logs) lives next to it. The target
repository itself carries nothing of ATeam's.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


def origin_url(root: Path) -> str | None:
    """The target's `origin` remote URL, or None when there is no remote or no repo."""
    if not root.is_dir():
        return None
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True)
    url = r.stdout.strip()
    return url if r.returncode == 0 and url else None


def normalise_remote(url: str) -> str:
    """`git@github.com:a/b.git`, `https://user:tok@GitHub.com/a/b/`, `ssh://git@github.com/a/b.git` -> `github.com/a/b`."""
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u)                 # scheme
    m = re.match(r"^([^@/]+@)?([^/:]+)[:/](.*)$", u)  # [user[:token]@]host[:/]path
    if not m:
        return u.rstrip("/").removesuffix(".git").lower()
    host, path = m.group(2).lower(), m.group(3)
    path = path.strip("/").removesuffix(".git").rstrip("/")
    return f"{host}/{path}"


def repo_id_source(root: Path) -> Literal["origin", "path"]:
    return "origin" if origin_url(root) else "path"


def repo_id(root: Path) -> str:
    """Stable short identifier: from the normalised origin URL, else from the resolved path."""
    url = origin_url(root)
    key = normalise_remote(url) if url else str(root.resolve())
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def home_dir(root: Path) -> Path:
    """ATeam's directory for a target: $ATEAM_HOME/<id> or ~/.ateam/<id>, created on call."""
    base = Path(os.environ.get("ATEAM_HOME") or (Path.home() / ".ateam"))
    path = base / repo_id(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class TargetPaths:
    """Locations for one target: the target root, its private intent repo, and scratch."""

    root: Path
    intent: Path
    records: Path
    contracts: Path
    plans: Path
    reports: Path
    config_file: Path
    scratch_root: Path

    @classmethod
    def from_root(cls, root: Path) -> TargetPaths:
        home = home_dir(root)
        intent = home / "intent"
        return cls(
            root=root,
            intent=intent,
            records=intent / "records",
            contracts=intent / "contracts",
            plans=intent / "plans",
            reports=intent / "reports",
            config_file=intent / "config.yaml",
            scratch_root=home / "scratch",
        )

    def exists(self) -> bool:
        """True when the intent repo has been initialised."""
        return (self.intent / ".git").exists()
