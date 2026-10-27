"""`ateam run`: exit codes, dry run, and the Ctrl-C handler (fake sessions; the real verify, merge and integrate)."""

import threading

import pytest

from ateam.config import NamedCheck
from test_integrate import _set_config, run
from test_scheduler import FakeWorkers, plan3  # noqa: F401  (fixture)


def _fake(monkeypatch, fake, investigate=None):
    from test_scheduler import FakeInvestigator
    monkeypatch.setattr("ateam.scheduler.default_launch", fake)
    monkeypatch.setattr("ateam.scheduler.default_investigate", investigate or FakeInvestigator())


def test_run_exit_codes(plan3, monkeypatch):
    _fake(monkeypatch, FakeWorkers())
    r = run("run", "P-0001", "--target", str(plan3.root))
    assert r.exit_code == 0, r.output
    assert "P-0001 -> merge_proposed" in r.output and "ateam merge P-0001" in r.output
    r = run("run", "P-0001", "--target", str(plan3.root))
    assert r.exit_code == 4 and "error: P-0001 is merge_proposed" in r.output


def test_run_paused_exits_1(plan3, monkeypatch):
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    _fake(monkeypatch, FakeWorkers({("C-0001", "build"): {"code": 3, "outcome": "scope_breach"}}))
    r = run("run", "P-0001", "--target", str(plan3.root))
    assert r.exit_code == 1 and "P-0001 -> paused (scope_exceeded" in r.output


def test_dry_run_command(plan3, monkeypatch):
    fake = FakeWorkers()
    _fake(monkeypatch, fake)
    r = run("run", "P-0001", "--dry-run", "--target", str(plan3.root))
    assert r.exit_code == 0 and "wave 1: C-0001, C-0003" in r.output and "worst case $" in r.output and fake.calls == []


def test_first_interrupt_sets_stop_and_a_second_exits_130():
    from ateam.cli import _interrupt_handler
    stop = threading.Event()
    said: list[str] = []
    handler = _interrupt_handler(stop, said.append)
    handler(2, None)
    assert stop.is_set() and "Ctrl-C again to exit now" in said[0]
    with pytest.raises(SystemExit) as e:
        handler(2, None)
    assert e.value.code == 130
