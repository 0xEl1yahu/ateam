"""Per-target configuration, stored as config.yaml in the target's private intent repo."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ateam.paths import TargetPaths


class NamedCheck(BaseModel):
    """A deterministic check: a name and the shell command that runs it."""

    name: str
    cmd: str


class TargetConfig(BaseModel):
    target_branch: str = "main"
    setup: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    checks: list[NamedCheck] = Field(default_factory=list)
    serialize_checks: bool = False
    protected_paths: list[str] = Field(default_factory=lambda: [".claude/**"])
    max_workers: int = 8   # a guard, not a limit (owner, 2026-10-28)
    run_budget_usd: float = 10.0   # `ateam run` stops launching sessions before this would be exceeded (owner, 2026-10-26)
    global_record_cap: int = 5
    tiers: dict[str, dict] = Field(default_factory=dict)
    log_retention_days: int = Field(default=14, ge=0)   # event logs of attempts finished longer ago are pruned (owner, 2026-10-29)
    brief_docs: list[str] = Field(default_factory=list)  # target-relative files included in investigation briefs


def load_config(paths: TargetPaths) -> TargetConfig:
    if not paths.config_file.is_file():
        return TargetConfig()
    data = yaml.safe_load(paths.config_file.read_text()) or {}
    return TargetConfig.model_validate(data)


def save_config(paths: TargetPaths, cfg: TargetConfig) -> Path:
    paths.intent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False))
    return paths.config_file
