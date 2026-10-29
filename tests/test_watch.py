"""`ateam watch`: tmux argv builders and the pane ticks. No real tmux runs; watch.run_tmux is replaced."""

import subprocess
from pathlib import Path

import pytest

from ateam import cli as cli_mod
from ateam import watch
from ateam.events import attempt_label
from ateam.gitops import GitRepo
from ateam.paths import TargetPaths, home_dir
from ateam.scheduler import ateam_argv
from ateam.state.db import StateDB
from test_integrate import run
from test_scheduler import plan3  # noqa: F401  (fixture)


class FakeTmux:
    def __init__(self, session_exists=False, windows=()):
        self.calls: list[list[str]] = []
        self.session_exists, self.windows = session_exists, list(windows)

    def __call__(self, argv):
        self.calls.append(list(argv))
        code, out = 0, ""
        if argv[0] == "has-session":
            code = 0 if self.session_exists else 1
        elif argv[0] == "list-windows":
            out = "\n".join(self.windows)
        return subprocess.CompletedProcess(["tmux", *argv], code, out, "")


@pytest.fixture
def tmux(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(watch, "run_tmux", fake)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tmux")
    monkeypatch.delenv("TMUX", raising=False)
    return fake


def _sdb(p):
    db = StateDB(home_dir(p.root) / "state.db")
    db.init_schema()
    return db


def test_watch_without_tmux_exits_nonzero_naming_ateam_log(plan3, monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(watch, "run_tmux", fake)
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = run("watch", "--target", str(plan3.root))
    assert r.exit_code == 1 and "ateam log A-<id> --follow" in r.output
    assert fake.calls == []


def test_watch_creates_session_with_status_and_pauses_windows(plan3, tmux):
    with _sdb(plan3) as db:
        aid = db.start_attempt("C-0001", "build", "light", "m", {})
    r = run("watch", "P-0001", "--target", str(plan3.root))
    assert r.exit_code == 0, r.output
    session = watch.session_name(plan3.paths, "P-0001")
    assert session.startswith("ateam-") and session.endswith("-P-0001")
    kinds = [c[0] for c in tmux.calls]
    assert kinds == ["has-session", "new-session", "new-window", "set-option", "new-window", "attach-session"]
    ateam = " ".join(ateam_argv())
    assert tmux.calls[1][:6] == ["new-session", "-d", "-s", session, "-n", "status"]
    assert tmux.calls[1][6].startswith(f"{ateam} watch P-0001 --pane status --target ")
    assert tmux.calls[2][-2] == "pauses" and f"watch P-0001 --pane pauses --target " in tmux.calls[2][-1]
    assert tmux.calls[3] == ["set-option", "-t", session, "remain-on-exit", "on"]
    assert tmux.calls[4][5] == attempt_label(aid)
    assert tmux.calls[5] == ["attach-session", "-t", session]


def test_switch_client_inside_tmux(plan3, tmux, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
    assert run("watch", "--target", str(plan3.root)).exit_code == 0
    assert tmux.calls[-1][0] == "switch-client"


def test_watch_attaches_to_existing_session(plan3, tmux):
    tmux.session_exists = True
    r = run("watch", "--target", str(plan3.root))
    assert r.exit_code == 0, r.output
    assert [c[0] for c in tmux.calls] == ["has-session", "attach-session"]


def test_attempt_window_follows_its_log(tmp_path):
    cmd = watch.attempt_window_command("s", ["/x/ateam"], tmp_path, 42)
    assert cmd == ["new-window", "-d", "-t", "s:", "-n", "A-0042", f"/x/ateam log A-0042 --follow --target {tmp_path}"]
    assert watch.attach_command("s", True) == ["switch-client", "-t", "s"]


def test_new_running_attempts_get_a_window_and_finished_ones_stay(plan3, tmux, capsys):
    with _sdb(plan3) as db:
        first = db.start_attempt("C-0001", "build", "light", "m", {})
    tmux.windows = ["status", "pauses", attempt_label(first)]
    cli_mod._status_pane_tick(plan3.root, "P-0001")
    assert not [c for c in tmux.calls if c[0] == "new-window"]
    with _sdb(plan3) as db:
        db.interrupt_attempt(first)
        second = db.start_attempt("C-0002", "build", "light", "m", {})
    cli_mod._status_pane_tick(plan3.root, "P-0001")
    out = capsys.readouterr().out
    assert attempt_label(second) in out and "P-0001" in out
    added = [c for c in tmux.calls if c[0] == "new-window"]
    assert [c[5] for c in added] == [attempt_label(second)]
    assert not [c for c in tmux.calls if c[0] in ("kill-window", "kill-session", "respawn-window")]


def test_status_pane_and_windows_watch_the_same_attempts(plan3, tmux, capsys):
    with _sdb(plan3) as db:
        mine = db.start_attempt("C-0001", "build", "light", "m", {})
        other = db.start_attempt("C-9999", "plan", "light", "m", {})   # a planning session, no plan contract
    for scope, expected in (("P-0001", [mine]), (None, [mine, other])):
        tmux.calls.clear()
        cli_mod._status_pane_tick(plan3.root, scope)
        out = capsys.readouterr().out
        lines = list(dict.fromkeys(x.split()[0] for x in out.splitlines() if x.startswith("    A-")))   # scoped status lists them too
        assert lines == [attempt_label(a) for a in expected]
        # no window exists yet, so the windows added are for the same set
        tmux.windows = ["status"]
        tmux.calls.clear()
        cli_mod._status_pane_tick(plan3.root, scope)
        capsys.readouterr()
        assert [c[5] for c in tmux.calls if c[0] == "new-window"] == [attempt_label(a) for a in expected]


def test_pauses_pane_lists_open_pauses(plan3, tmux, capsys):
    cli_mod._pauses_pane_tick(plan3.root, None)
    assert "No pause is waiting" in capsys.readouterr().out
    with _sdb(plan3) as db:
        db.record_pause("P-0001", "scope_exceeded", "detail", [])
    cli_mod._pauses_pane_tick(plan3.root, None)
    out = capsys.readouterr().out
    assert "P-0001" in out and "scope_exceeded" in out and "ateam status P-0001" in out


def _snapshot(p):
    with _sdb(p) as db:
        tables = {t: [tuple(r) for r in db.conn.execute(f"SELECT * FROM {t}")]
                  for t in ("attempts", "transitions", "pauses", "run_lock")}
    git = GitRepo(p.paths.intent)
    return tables, git.head(), subprocess.run(["git", "-C", str(p.paths.intent), "status", "--porcelain"],
                                              capture_output=True, text=True).stdout


def test_watch_changes_nothing(plan3, tmux, capsys):
    with _sdb(plan3) as db:
        db.start_attempt("C-0001", "build", "light", "m", {})
        db.record_pause("P-0001", "scope_exceeded", "detail", [])
    before = _snapshot(plan3)
    assert run("watch", "P-0001", "--target", str(plan3.root)).exit_code == 0
    cli_mod._status_pane_tick(plan3.root, "P-0001")
    cli_mod._pauses_pane_tick(plan3.root, "P-0001")
    assert _snapshot(plan3) == before
    assert not [c for c in tmux.calls if c[0].startswith("kill")]


def test_watch_module_is_independent_of_cli():
    src = Path(watch.__file__).read_text()
    assert "ateam.cli" not in src and "import cli" not in src
