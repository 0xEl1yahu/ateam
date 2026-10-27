"""Opt-in: pause, investigate and resume with real light-tier sessions. Costs cents. Set ATEAM_LIVE_TESTS=1.

Not run by default. The worktree setup command fails, so the run pauses with environment_failure without a BUILD
session; the automatic pause investigation is a real session that must read the contract worktree under ATeam's home
(the own-worktree policy). The owner then fixes setup and resumes with a note, and the run reaches merge_proposed."""

import os
import subprocess

import pytest

from ateam.config import NamedCheck
from ateam.paths import home_dir
from ateam.state.db import StateDB
from test_integrate import _set_config
from test_scheduler import plan3  # noqa: F401  (fixture: three contracts, light tiers)

pytestmark = pytest.mark.skipif(not os.environ.get("ATEAM_LIVE_TESTS"), reason="set ATEAM_LIVE_TESTS=1")

HAIKU = {"model": "haiku", "effort": "low", "max_turns": 30, "usd": 1.0}


def _ateam(*args, timeout):
    from ateam.scheduler import ateam_argv
    return subprocess.run([*ateam_argv(), *args], capture_output=True, text=True, timeout=timeout)


def test_pause_investigate_resume_to_merge_proposed(plan3):
    root = str(plan3.root)
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1, setup=["false"],
                tiers={"careful": {"investigate": HAIKU}})
    r = _ateam("run", "P-0001", "--target", root, timeout=900)
    print(r.stdout)
    assert r.returncode == 1 and "paused (environment_failure" in r.stdout, r.stdout + r.stderr

    db = StateDB(home_dir(plan3.root) / "state.db"); db.init_schema()
    pause = db.open_pause("P-0001")
    [paused] = pause["contracts"]
    investigations = [a for a in db.attempts_for(paused["contract_id"]) if a["role"] == "investigate"]
    assert investigations and investigations[-1]["outcome"] == "completed", investigations
    assert paused["findings"] and paused["findings"]["summary"], paused
    status = _ateam("status", "P-0001", "--target", root, timeout=60)
    print(status.stdout)
    assert "findings:" in status.stdout and "ateam resume P-0001" in status.stdout

    _set_config(plan3.paths, setup=[])                    # the owner fixes the environment
    r2 = _ateam("resume", "P-0001", "--note", "setup is fixed now; implement the contract as written", "--target", root,
                timeout=1800)
    print(r2.stdout)
    assert r2.returncode == 0 and "P-0001 -> merge_proposed" in r2.stdout, r2.stdout + r2.stderr
