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


def test_target_claude_md_mentions_every_command_it_lists_exists():
    import re
    from click.testing import CliRunner

    from ateam.cli import main

    text = (EXAMPLES / "CLAUDE.target.md").read_text()
    commands = sorted({m.group(1) for m in re.finditer(r"^ateam ([a-z]+(?: [a-z]+)?)", text, re.M)})
    assert commands, "no commands found in the snippet"
    for cmd in commands:
        r = CliRunner().invoke(main, [*cmd.split(), "--help"])
        assert r.exit_code == 0, f"`ateam {cmd}` is not a real command"


def test_plan_example_loads():
    from ateam.plans.model import Plan

    p = Plan.model_validate(yaml.safe_load((EXAMPLES / "plan.example.yaml").read_text()))
    assert p.id == "P-0001" and p.contracts and p.status == "plan_proposed"


def test_examples_carry_interfaces():
    c = Contract.model_validate(yaml.safe_load((EXAMPLES / "contract.example.yaml").read_text()))
    assert {i.kind for i in c.interfaces} == {"produces", "consumes"}
