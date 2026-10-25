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
    (root / ".gitignore").write_text(".ateam-scratch/\n__pycache__/\n")
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


@pytest.fixture
def records_three(sample_record):
    """R-0001 approved binds src/api/**; R-0002 approved global; R-0003 proposed binds src/**."""
    from ateam.records.model import Binds, Evidence, Origin, Status, compute_fingerprint

    r1 = sample_record.model_copy(update={"status": Status.approved, "binds": Binds(paths=["src/api/**"])})
    ev2 = [Evidence(type="doc", ref="README.md#principles")]
    r2 = sample_record.model_copy(update={
        "id": "R-0002", "status": Status.approved, "statement": "No analytics or third-party requests at runtime",
        "evidence": ev2, "fingerprint": compute_fingerprint(ev2), "binds": Binds(), "serves": ["privacy"],
    })
    ev3 = [Evidence(type="code", ref="src/app.py:1")]
    r3 = sample_record.model_copy(update={
        "id": "R-0003", "status": Status.proposed, "origin": Origin.inferred, "statement": "Modules under src expose a greet function",
        "evidence": ev3, "fingerprint": compute_fingerprint(ev3), "binds": Binds(paths=["src/**"]), "serves": ["friendly api"],
    })
    return {"R-0001": r1, "R-0002": r2, "R-0003": r3}


@pytest.fixture
def record_service(target_repo, sample_record):
    """RecordService over target_repo with sample_record saved and committed as proposed."""
    from ateam.config import TargetConfig
    from ateam.gitops import GitRepo
    from ateam.paths import TargetPaths
    from ateam.records.service import RecordService
    from ateam.records.store import save_record

    save_record(target_repo / ".ateam/records", sample_record)
    git = GitRepo(target_repo)
    git.commit_paths([".ateam"], "seed record")
    return RecordService(TargetPaths.from_root(target_repo), git, TargetConfig())


@pytest.fixture
def sample_contract():
    from ateam.contracts.model import Acceptance, Allocation, Check, Contract, Level

    return Contract(
        id="C-0001",
        plan="P-0001",
        title="Expose reading state through the books API",
        paths=["src/api/**", "tests/api/test_books.py"],
        incidental=["requirements.txt"],
        acceptance=[
            Acceptance(id="A1", statement="GET /books/{id} returns reading_state", check=Check(type="test", ref="tests/api/test_books.py::test_reading_state_present")),
            Acceptance(id="A3", statement="No code path infers reading state", check=Check(type="judgment")),
        ],
        allocation=Allocation(
            complexity=Level.medium, risk=Level.high, uncertainty=Level.low,
            reasoning="API boundary bound by R-0001; API untested; requirements explicit.",
            tier={"build": "standard", "test": "careful", "investigate": "none"},
        ),
    )


@pytest.fixture
def cli_target(target_repo, ateam_home, sample_record, sample_contract):
    """target_repo with sample_record (proposed) and sample_contract committed; ATEAM_HOME isolated."""
    from ateam.contracts.store import save_contract
    from ateam.gitops import GitRepo
    from ateam.records.store import save_record

    save_record(target_repo / ".ateam/records", sample_record)
    save_contract(target_repo / ".ateam/contracts", sample_contract)
    GitRepo(target_repo).commit_paths([".ateam"], "seed")
    return target_repo


def _policy(target_repo, records_three, role):
    from ateam.runner.policy import Policy

    return Policy(
        worktree=target_repo, role=role, allowed_paths=["src/api/**"],
        protected=[".ateam/**", ".claude/**"], bound={"src/api/**": [records_three["R-0001"]]},
    )


@pytest.fixture
def policy_build(target_repo, records_three):
    from ateam.roles import Role

    return _policy(target_repo, records_three, Role.build)


@pytest.fixture
def policy_test(target_repo, records_three):
    from ateam.roles import Role

    return _policy(target_repo, records_three, Role.test)


@pytest.fixture
def policy_investigate(target_repo, records_three):
    from ateam.roles import Role

    return _policy(target_repo, records_three, Role.investigate)
