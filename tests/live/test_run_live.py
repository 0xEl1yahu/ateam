"""Opt-in: `ateam run` with a real light-tier session, interrupted during BUILD. Costs cents. Set ATEAM_LIVE_TESTS=1.

Not run by default. Checks that a terminal Ctrl-C reaches the BUILD session, the attempt ends `interrupted`, the plan
stays executing and the run lock is released."""

import os

from test_scheduler import plan3  # noqa: F401  (fixture: three contracts, light tiers)
import signal
import subprocess
import time

import pytest

from ateam.paths import home_dir
from ateam.state.db import StateDB

pytestmark = pytest.mark.skipif(not os.environ.get("ATEAM_LIVE_TESTS"), reason="set ATEAM_LIVE_TESTS=1")


def test_ctrl_c_during_build_interrupts_the_attempt(plan3):
    from ateam.scheduler import ateam_argv
    proc = subprocess.Popen([*ateam_argv(), "run", "P-0001", "--target", str(plan3.root)], start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    db = StateDB(home_dir(plan3.root) / "state.db"); db.init_schema()
    deadline = time.time() + 120
    while time.time() < deadline and not db.open_attempts(["C-0001", "C-0002", "C-0003"]):
        time.sleep(0.5)
    assert db.open_attempts(["C-0001", "C-0002", "C-0003"]), "no BUILD session started"
    time.sleep(3)
    os.killpg(proc.pid, signal.SIGINT)                     # what a terminal Ctrl-C does: the whole process group
    out, _ = proc.communicate(timeout=300)
    assert proc.returncode == 130, out
    assert db.open_attempts(["C-0001", "C-0002", "C-0003"]) == []
    assert any(a["outcome"] == "interrupted" for c in ("C-0001", "C-0003") for a in db.attempts_for(c))
    assert db.feature_status("P-0001") == "executing" and db.run_lock_info() is None
