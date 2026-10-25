import os
import time

import pytest

from ateam.config import NamedCheck
from ateam.contracts.model import Acceptance, Check
from ateam.treecheck import ScopeResult
from ateam.verify import (
    AcceptanceResult, CheckResult, Snapshot, acceptance_presence, check_env, compare, decide, run_checks, walk_to,
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


def test_run_checks_does_not_wait_for_background_children(tmp_path):
    start = time.monotonic()
    [r] = run_checks([_check("bg", "sleep 5 & exit 0")], tmp_path, dict(os.environ), timeout=20)
    assert r.passed and time.monotonic() - start < 3


def test_run_checks_kills_background_children_when_the_check_ends(tmp_path):
    [r] = run_checks([_check("bg", "(sleep 1; touch late) >/dev/null 2>&1 & exit 0")], tmp_path, dict(os.environ))
    assert r.passed
    time.sleep(2)
    assert not (tmp_path / "late").exists()


def test_run_checks_kills_the_group_when_interrupted(tmp_path, monkeypatch):
    import subprocess
    pidfile = tmp_path / "pid"

    def interrupted(self, timeout=None):
        time.sleep(0.5)
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(subprocess.Popen, "wait", interrupted)
        with pytest.raises(KeyboardInterrupt):
            run_checks([_check("slow", f"sleep 30 & echo $! > {pidfile}; wait")], tmp_path, dict(os.environ))
    pid = int(pidfile.read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("Ctrl-C left the check running")


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
        "SSH_AUTH_SOCK": "/s", "MYSQL_PASSWD": "p", "AWS_CREDS": "c", "DB_PWD": "p", "OLDPWD": "/o", "PWD": "/w",
        "GIT_DIR": "/g", "GIT_WORK_TREE": "/t", "GIT_INDEX_FILE": "/i", "GIT_AUTHOR_NAME": "a",
    }
    env = check_env(base, {"FOO": "bar", "PATH": "/custom"})
    assert env == {"PATH": "/custom", "HOME": "/h", "LANG": "C", "FOO": "bar", "OLDPWD": "/o", "PWD": "/w",
                   "GIT_AUTHOR_NAME": "a"}


def test_check_env_drops_the_launching_virtualenv_from_path():
    env = check_env({"PATH": "/v/bin:/usr/bin:/v/bin/:/bin", "VIRTUAL_ENV": "/v"}, {})
    assert env["PATH"] == "/usr/bin:/bin"


def test_check_env_lets_owner_config_add_a_scrubbed_name_back():
    assert check_env({"X": "1"}, {"NPM_TOKEN": "owner-set"})["NPM_TOKEN"] == "owner-set"


def _acc(aid, type_, ref=None):
    return Acceptance(id=aid, statement="s", check=Check(type=type_, ref=ref))


def _with(sample_contract, *acceptance):
    return sample_contract.model_copy(update={"acceptance": list(acceptance)})


def test_acceptance_presence(sample_contract, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_present():\n    pass\n\n"
        "# def test_commented(): a comment is not a definition\n"
        "X = 'test_in_string'\n\n"
        "class TestGroup:\n    async def test_method(self):\n        pass\n"
    )
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
        _acc("A10", "test", "tests/test_a.py::test_commented"),
        _acc("A11", "test", "tests/test_a.py::test_in_string"),
        _acc("A12", "test", "tests/test_a.py::TestGroup::test_method"),
        _acc("A13", "test", "tests/test_a.py::TestGroup::test_nope"),
        _acc("A14", "test", "::test_present"),
    )
    (tmp_path.parent / "outside.py").write_text("def test_x(): pass\n")
    got = {r.id: r.ok for r in acceptance_presence(c, tmp_path)}
    assert got == {"A1": True, "A2": False, "A3": False, "A4": False, "A5": False, "A6": False,
                   "A7": None, "A8": None, "A9": False, "A10": False, "A11": False, "A12": True, "A13": False,
                   "A14": False}


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


_BASE = Snapshot("w1", (), (), "i1", (), "o1", (), "db1")


@pytest.mark.parametrize("field, value, label", [
    ("worktree_head", "w2", "worktree HEAD moved"),
    ("worktree_tracked", ((" M", "src/a.py"),), "worktree tracked files changed"),
    ("intent_head", "i2", "intent repo HEAD moved"),
    ("intent_dirty", (("??", "planted"),), "intent repo files changed"),
    ("owner_head", "o2", "owner checkout HEAD moved"),
    ("db", "db2", "state store changed"),
])
def test_compare_tamper_labels(field, value, label):
    from dataclasses import replace
    tamper, warnings = compare(_BASE, replace(_BASE, **{field: value}))
    assert tamper == [label] and warnings == []


def test_compare_untracked_output_and_owner_edits_are_warnings_only():
    from dataclasses import replace
    after = replace(_BASE, worktree_untracked=(("??", "build/out.o"),), owner_dirty=((" M", "notes.md"),))
    tamper, warnings = compare(_BASE, after)
    assert tamper == []
    assert any("build/out.o" in w for w in warnings) and any("your checkout" in w for w in warnings)


@pytest.mark.parametrize("current, final, walk", [
    ("ready", "verified", ["building", "verifying", "verified"]),
    ("building", "failed", ["verifying", "failed"]),
    ("failed", "verified", ["building", "verifying", "verified"]),
    ("verifying", "failed", ["failed"]),
])
def test_walk_to(current, final, walk):
    assert walk_to(current, final) == walk
