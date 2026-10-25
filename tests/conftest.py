"""Shared fixtures: a scratch target repo and an isolated ATeam home."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one commit, `src/`, `tests/` and an empty `.ateam/`."""
    root = tmp_path / "target"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "tests")
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    (root / "src" / "api" / "x.py").write_text("ROUTES = []\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_greet():\n    assert True\n")
    (root / ".ateam" / "records").mkdir(parents=True)
    (root / ".ateam" / "contracts").mkdir()
    (root / ".ateam" / "records" / ".gitkeep").write_text("")
    (root / ".ateam" / "contracts" / ".gitkeep").write_text("")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def ateam_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ateam-home"
    monkeypatch.setenv("ATEAM_HOME", str(home))
    return home


@pytest.fixture
def sample_record():
    from datetime import UTC, datetime

    from ateam.records.model import (
        Basis, Binds, Evidence, HistoryEntry, Kind, Origin, Record, Status, compute_fingerprint,
    )

    evidence = [Evidence(type="doc", ref="docs/spec.md#anti-goals")]
    return Record(
        id="R-0001",
        kind=Kind.invariant,
        status=Status.proposed,
        origin=Origin.documented,
        basis=Basis.requirement,
        statement="Reading state is only ever set by the owner",
        rationale="Inferred progress produced false confidence before.",
        evidence=evidence,
        binds=Binds(paths=["src/data/books.py", "src/api/**"]),
        serves=["owner controls their own data"],
        fingerprint=compute_fingerprint(evidence),
        history=[HistoryEntry(at=datetime(2026, 10, 24, 10, 12, tzinfo=UTC), by="ateam-init", to=Status.proposed, why="found in spec")],
    )
