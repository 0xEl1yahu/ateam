"""The shipped examples must always load through the real models."""

from pathlib import Path

import yaml

from ateam.config import TargetConfig
from ateam.contracts.model import Contract
from ateam.records.model import Record, compute_fingerprint

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_record_example_loads_and_fingerprint_matches():
    r = Record.model_validate(yaml.safe_load((EXAMPLES / "record.example.yaml").read_text()))
    assert r.id == "R-0001" and r.fingerprint == compute_fingerprint(r.evidence) and r.binds.paths


def test_contract_example_loads():
    c = Contract.model_validate(yaml.safe_load((EXAMPLES / "contract.example.yaml").read_text()))
    assert c.id == "C-0001" and c.acceptance and set(c.allocation.tier) == {"build", "test", "investigate"}


def test_config_example_loads():
    cfg = TargetConfig.model_validate(yaml.safe_load((EXAMPLES / "config.example.yaml").read_text()))
    assert cfg.checks and cfg.target_branch
