import os
import time

import pytest

from ateam.config import NamedCheck
from ateam.contracts.model import Acceptance, Check
from ateam.treecheck import ScopeResult
from ateam.verify import (
    AcceptanceResult, CheckResult, Snapshot, acceptance_presence, check_env, decide, run_checks, tampered, walk_to,
)


def _check(name, cmd):
    return NamedCheck(name=name, cmd=cmd)


@pytest.mark.parametrize("cmd, passed, errored, cls", [
    ("true", True, False, None),
    ("false", False, False, "implementation"),
    ("exit 3", False, False, "implementation"),
    ("exit 127", False, True, "environment"),
    ("exit 126", False, True, "environment"),
    ("definitely-not-a-command-xyz", False, True, "environment"),
])
def test_run_checks_classifies_exit_codes(tmp_path, cmd, passed, errored, cls):
    [r] = run_checks([_check("c", cmd)], tmp_path, dict(os.environ))
    assert (r.passed, r.errored, r.failure_class) == (passed, errored, cls)


def test_run_checks_runs_every_check_in_the_worktree_and_keeps_a_tail(tmp_path):
    results = run_checks([_check("a", "false"), _check("b", "pwd; printf 'x%.0s' $(seq 1 5000)")], tmp_path, dict(os.environ))
    assert [r.name for r in results] == ["a", "b"]
    assert results[1].passed and len(results[1].tail) <= 4000 and results[1].tail.endswith("x")
    assert str(tmp_path.resolve()) in run_checks([_check("p", "pwd")], tmp_path, dict(os.environ))[0].tail


def test_run_checks_timeout_is_environment_and_kills_the_process_group(tmp_path):
    pidfile = tmp_path / "pid"
    [r] = run_checks([_check("slow", f"sleep 30 & echo $! > {pidfile}; wait")], tmp_path, dict(os.environ), timeout=1)
    assert r.errored and r.failure_class == "environment" and "timed out" in r.tail
    pid = int(pidfile.read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild sleep survived the timeout")


def test_check_env_scrubs_credentials_and_python_paths():
    base = {
        "PATH": "/usr/bin", "HOME": "/h", "CLAUDE_CODE_X": "1", "ANTHROPIC_API_KEY": "k", "anthropic_base": "u",
        "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "DB_PASSWORD": "p", "GOOGLE_CREDENTIALS": "c",
        "my_api_key": "k2", "VIRTUAL_ENV": "/v", "PYTHONPATH": "/p", "PYTHONHOME": "/ph", "LANG": "C",
    }
    env = check_env(base, {"FOO": "bar", "PATH": "/custom"})
    assert env == {"PATH": "/custom", "HOME": "/h", "LANG": "C", "FOO": "bar"}


def test_check_env_lets_owner_config_add_a_scrubbed_name_back():
    assert check_env({"X": "1"}, {"NPM_TOKEN": "owner-set"})["NPM_TOKEN"] == "owner-set"


def _acc(aid, type_, ref=None):
    return Acceptance(id=aid, statement="s", check=Check(type=type_, ref=ref))


def _with(sample_contract, *acceptance):
    return sample_contract.model_copy(update={"acceptance": list(acceptance)})


def test_acceptance_presence(sample_contract, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_present():\n    pass\n\ndef test_present_more():\n    pass\n")
    c = _with(
        sample_contract,
        _acc("A1", "test", "tests/test_a.py::test_present"),
        _acc("A2", "test", "tests/test_a.py::test_absent"),
        _acc("A3", "test", "tests/missing.py::test_x"),
        _acc("A4", "test", "../outside.py::test_x"),
        _acc("A5", "test", "/etc/passwd::root"),
        _acc("A6", "test", None),
        _acc("A7", "judgment"),
        _acc("A8", "command", "make lint"),
        _acc("A9", "test", "tests/test_a.py::test_pres"),
    )
    (tmp_path.parent / "outside.py").write_text("def test_x(): pass\n")
    got = {r.id: r.ok for r in acceptance_presence(c, tmp_path)}
    assert got == {"A1": True, "A2": False, "A3": False, "A4": False, "A5": False, "A6": False,
                   "A7": None, "A8": None, "A9": False}


def _scope(ok=True):
    return ScopeResult(ok=ok, outside=[] if ok else ["elsewhere.py"], dirty=[])


def _res(name, passed, errored=False):
    return CheckResult(name=name, cmd="x", passed=passed, errored=errored, exit_code=0 if passed else 1, tail="")


def test_decide_verified_only_when_everything_holds():
    ok = [AcceptanceResult("A1", "test", True, ""), AcceptanceResult("A2", "judgment", None, "")]
    assert decide(_scope(), [_res("unit", True)], ok, []).status == "verified"
    out = decide(_scope(False), [_res("unit", True)], ok, [])
    assert (out.status, out.failure_class) == ("failed", "implementation") and "elsewhere.py" in " ".join(out.reasons)
    out = decide(_scope(), [_res("unit", True)], [AcceptanceResult("A1", "test", False, "not found")], [])
    assert (out.status, out.failure_class) == ("failed", "implementation")


def test_decide_class_order_tamper_then_environment_then_implementation():
    acc = [AcceptanceResult("A1", "test", False, "")]
    assert decide(_scope(False), [_res("a", False), _res("b", False, errored=True)], acc, ["intent repo changed"]).failure_class == "tamper"
    assert decide(_scope(False), [_res("a", False), _res("b", False, errored=True)], acc, []).failure_class == "environment"
    assert decide(_scope(), [_res("a", False)], [], []).failure_class == "implementation"


def test_tampered_names_every_change():
    before = Snapshot("w1", (), "i1", True, "o1", ())
    assert tampered(before, before) == []
    after = Snapshot("w2", (("??", "new"),), "i2", False, "o2", ((" M", "f"),))
    assert len(tampered(before, after)) == 6


@pytest.mark.parametrize("current, final, walk", [
    ("ready", "verified", ["building", "verifying", "verified"]),
    ("building", "failed", ["verifying", "failed"]),
    ("failed", "verified", ["building", "verifying", "verified"]),
    ("verifying", "failed", ["failed"]),
])
def test_walk_to(current, final, walk):
    assert walk_to(current, final) == walk
