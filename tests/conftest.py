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
