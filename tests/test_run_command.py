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
    assert r.exit_code == 0 and "lane 1: C-0001\n" in r.output and "lane 2: C-0002\n" in r.output and "lane 3: C-0003" in r.output and "worst case $" in r.output and fake.calls == []


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


# ---- ateam resume (pause and resume, task 7)

import os  # noqa: E402

from ateam.gitops import GitRepo  # noqa: E402
from ateam.paths import home_dir  # noqa: E402
from ateam.state.db import StateDB  # noqa: E402
from test_scheduler import FakeInvestigator  # noqa: E402

_SCOPE = {("C-0001", "build"): {"code": 3, "outcome": "scope_breach", "commit": False}}


def _sdb(p):
    db = StateDB(home_dir(p.root) / "state.db"); db.init_schema()
    return db


def _paused(p, monkeypatch, investigate=None):
    """P-0001 paused by C-0001 (scope_exceeded after 2 BUILD attempts), with an investigation's findings recorded."""
    _set_config(p.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    _fake(monkeypatch, FakeWorkers(_SCOPE), investigate)
    r = run("run", "P-0001", "--target", str(p.root))
    assert r.exit_code == 1 and "paused (scope_exceeded" in r.output, r.output
    _fake(monkeypatch, FakeWorkers())
    return _sdb(p)


def _intent_head(p):
    return GitRepo(p.paths.intent).head()


def _resume(p, *args):
    return run("resume", "P-0001", *args, "--target", str(p.root))


def _seed(p, contracts, reason="budget_exhausted", conflict=False):
    return _sdb(p).record_pause("P-0001", reason, "seeded", contracts, conflict=conflict)


def _entry(cid, h, findings=None):
    return {"contract_id": cid, "reason": "budget_exhausted", "detail": "d", "contract_hash": h, "evidence": None}


def _not_paused(p, mp):
    _set_config(p.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    return (), "only a paused plan resumes"


def _lock_held(p, mp):
    _paused(p, mp)
    with _sdb(p).conn as c:
        c.execute("INSERT INTO run_lock (singleton, plan_id, acquired_at, pid) VALUES (1, 'P-0001', 'then', ?)", (os.getpid(),))
    return (), "is running"


def _planning_live(p, mp):
    db = _paused(p, mp)
    db.transition("feature", "P-0099", "requested", by="t"); db.transition("feature", "P-0099", "investigating", by="t")
    db.set_meta("planning:P-0099", str(os.getpid()))
    return (), "is being planned"


def _dirty_intent(p, mp):
    _paused(p, mp)
    (p.paths.intent / "stray.txt").write_text("x\n")
    return (), "uncommitted changes"


def _conflict(p, mp):
    _paused(p, mp)   # a moved target branch conflicts: the pause names no contract, so nothing can be rebuilt
    _seed(p, [], reason="environment_failure", conflict=True)
    return (), "resume cannot rebuild it"


def _hand_edit(p, mp):
    from ateam.contracts.store import load_contracts, save_contract
    from ateam.intent import commit_intent
    _paused(p, mp)
    c = load_contracts(p.paths.contracts)["C-0003"]
    save_contract(p.paths.contracts, c.model_copy(update={"title": "edited by hand"}))
    commit_intent(p.paths, "hand edit", ["contracts"])
    return (), "C-0003 differs from its frozen hash"


def _adopt_without_suggestion(p, mp):
    _paused(p, mp, FakeInvestigator(outcome="failed"))
    return ("--adopt",), "no suggestion for C-0001"


def _adopt_after_owner_stop(p, mp):
    _paused(p, mp)
    _seed(p, [], reason="owner_stopped")
    return ("--adopt",), "nothing to adopt"


def _adopt_before_refreeze(p, mp):
    db = _paused(p, mp)
    pid = _seed(p, [_entry("C-0001", "an-older-hash")])
    db.set_pause_findings(pid, "C-0001", 42, {"summary": "s", "suggested_instruction": "use x"}, None)
    return ("--adopt",), "written before its re-freeze"


def _adopt_and_note(p, mp):
    _paused(p, mp)
    return ("--adopt", "--note", "x"), "not both"


def _note_two_paused(p, mp):
    db = _paused(p, mp)
    _seed(p, [_entry("C-0001", db.contract_hash("C-0001")), _entry("C-0003", db.contract_hash("C-0003"))])
    return ("--note", "x"), "--contract"


def _note_all_integrated(p, mp):
    db = _paused(p, mp)
    with db.conn as c:
        c.execute("UPDATE contracts SET status='integrated'")
    _seed(p, [])
    return ("--note", "x", "--contract", "C-0001"), "is integrated; an instruction has no BUILD"


@pytest.mark.parametrize("setup", [_not_paused, _lock_held, _planning_live, _dirty_intent, _conflict, _hand_edit,
                                   _adopt_without_suggestion, _adopt_after_owner_stop, _adopt_before_refreeze,
                                   _adopt_and_note, _note_two_paused, _note_all_integrated])
def test_resume_refusals(setup, plan3, monkeypatch):
    args, why = setup(plan3, monkeypatch)
    status, head = _sdb(plan3).feature_status("P-0001"), _intent_head(plan3)
    r = _resume(plan3, *args)
    assert r.exit_code == 4 and why in r.output, r.output
    assert _sdb(plan3).feature_status("P-0001") == status and _intent_head(plan3) == head


def test_resume_with_a_note_commits_guidance_and_runs_to_merge_proposed(plan3, monkeypatch):
    _paused(plan3, monkeypatch)
    r = _resume(plan3, "--note", "use x")
    assert r.exit_code == 0 and "P-0001 -> merge_proposed" in r.output, r.output
    g = GitRepo(plan3.paths.intent)
    sha = g.run("log", "--format=%H", "--grep", "P-0001 -> executing", "-1").strip()
    changed = g.run("show", "--name-only", "--format=", sha).split()
    assert any(f.startswith("plans/") for f in changed) and "guidance/C-0001.yaml" in changed
    assert "owner: use x" in run("brief", "C-0001", "--role", "build", "--target", str(plan3.root)).output


def test_resume_adopt_records_the_investigation_source(plan3, monkeypatch):
    from ateam.guidance import load_guidance
    db = _paused(plan3, monkeypatch)
    pause = db.open_pause("P-0001")
    db.set_pause_findings(pause["pause_id"], "C-0001", 42, {"summary": "s", "suggested_instruction": "use the helper"}, None)
    r = _resume(plan3, "--adopt", "--no-run")
    assert r.exit_code == 0, r.output
    [e] = load_guidance(plan3.paths.guidance, "C-0001")
    assert e.source == "A-42" and e.text == "use the helper" and e.contract_hash == db.contract_hash("C-0001")


def test_resume_no_run_only_changes_state(plan3, monkeypatch):
    db = _paused(plan3, monkeypatch)
    fake = FakeWorkers(); _fake(monkeypatch, fake)
    r = _resume(plan3, "--no-run")
    assert r.exit_code == 0 and "P-0001 -> executing" in r.output and fake.calls == []
    assert db.feature_status("P-0001") == "executing" and db.open_pause("P-0001") is None and db.window_start("C-0001") > 0


def test_resume_after_a_failed_intent_commit_resumes_again(plan3, monkeypatch):
    import subprocess

    from ateam.guidance import load_guidance
    from ateam.intent import IntentError
    db = _paused(plan3, monkeypatch)
    real = __import__("ateam.plans.ops", fromlist=["commit_intent_files"]).commit_intent_files
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntentError("gpg failed to sign the data")
        return real(*a, **kw)

    monkeypatch.setattr("ateam.plans.ops.commit_intent_files", flaky)
    r = _resume(plan3, "--note", "use x", "--no-run")
    assert r.exit_code == 1 and "gpg failed" in r.output and "git -C" in r.output, r.output
    assert db.feature_status("P-0001") == "paused"
    for line in r.output.splitlines():
        if line.strip().startswith("git -C"):
            subprocess.run(line.strip(), shell=True, check=True)
    r2 = _resume(plan3, "--note", "use x", "--no-run")
    assert r2.exit_code == 0, r2.output
    assert [e.text for e in load_guidance(plan3.paths.guidance, "C-0001")] == ["use x"]


def test_resume_of_a_plan_paused_without_a_record(plan3, monkeypatch):
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans
    db = _sdb(plan3)
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["executing", "paused"], by="t", why="budget_exhausted: old")
    r = _resume(plan3, "--no-run")
    assert r.exit_code == 0 and "no contract's attempts were refreshed" in r.output, r.output
    assert db.feature_status("P-0001") == "executing" and all(db.window_start(c) == 0 for c in ("C-0001", "C-0002", "C-0003"))


def test_refrozen_contract_gets_a_window_and_no_stale_guidance(plan3, monkeypatch):
    from datetime import UTC, datetime

    from ateam.contracts.store import load_contracts, save_contract
    from ateam.guidance import GuidanceEntry, append_guidance
    from ateam.intent import commit_intent
    db = _paused(plan3, monkeypatch)
    c = load_contracts(plan3.paths.contracts)["C-0001"]
    append_guidance(plan3.paths.guidance, "C-0001", GuidanceEntry(at=datetime(2026, 10, 27, tzinfo=UTC), source="owner",
                                                                   contract_hash=c.content_hash(), text="for the old contract"))
    save_contract(plan3.paths.contracts, c.model_copy(update={"title": "Add a, clarified"}))
    commit_intent(plan3.paths, "owner clarifies C-0001", ["contracts", "guidance"])
    assert run("contracts", "freeze", "C-0001", "--force", "--target", str(plan3.root)).exit_code == 0
    r = _resume(plan3, "--no-run")
    assert r.exit_code == 0, r.output
    assert db.window_start("C-0001") > 0
    assert "Owner's instructions" not in run("brief", "C-0001", "--role", "build", "--target", str(plan3.root)).output


# ---- ateam stop and the paused-plan status view (pause and resume, task 8)

def _executing(p):
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans
    db = _sdb(p)
    set_plan_status(p.paths, db, load_plans(p.paths.plans)["P-0001"], ["executing"], by="t")
    return db


def test_stop_from_another_process_pauses_a_running_scheduler(plan3):
    import time

    from ateam.cli import Context
    from ateam.scheduler import run_plan
    _set_config(plan3.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    stop = threading.Event()

    def blocks_until_stopped(fake, cid, role, worktree, n):
        deadline = time.monotonic() + 120
        while not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)
        return {"code": 130, "outcome": "interrupted", "commit": False}

    box: dict = {}
    t = threading.Thread(target=lambda: box.update(s=run_plan(lambda: Context(plan3.root), "P-0001", lambda m: None, stop,
                                                              launch=FakeWorkers({("C-0001", "build", 0): blocks_until_stopped}),
                                                              investigate=FakeInvestigator())))
    t.start()
    deadline = time.monotonic() + 120      # generous: under a loaded full suite the BUILD can take well over 10 s to start
    while not _sdb(plan3).open_attempts(["C-0001"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _sdb(plan3).open_attempts(["C-0001"]), "the BUILD never started; the stop would not test an interrupt"
    r = run("stop", "P-0001", "--target", str(plan3.root))
    t.join(120)
    db = _sdb(plan3)
    assert r.exit_code == 0 and "stop requested" in r.output, r.output
    assert box["s"].outcome == "paused" and db.transitions("feature", "P-0001")[-1]["reason"] == "owner_stopped: stopped by the owner"
    assert db.attempts_for("C-0001", role="build")[0]["outcome"] == "interrupted" and db.run_lock_info() is None


def test_stop_without_a_run_pauses_directly(plan3):
    db = _executing(plan3)
    head = _intent_head(plan3)
    r = run("stop", "P-0001", "--target", str(plan3.root))
    assert r.exit_code == 0 and "P-0001 -> paused (owner_stopped)" in r.output, r.output
    assert db.feature_status("P-0001") == "paused" and db.open_pause("P-0001")["contracts"] == []
    assert GitRepo(plan3.paths.intent).run("rev-list", "--count", f"{head}..HEAD").strip() == "1" and db.run_lock_info() is None


@pytest.mark.parametrize("case", ["not_executing", "other_plan", "stale_lock", "in_progress"])
def test_stop_refusals(case, plan3):
    db = _sdb(plan3) if case == "not_executing" else _executing(plan3)
    lock = {"other_plan": ("P-0042", os.getpid()), "stale_lock": ("P-0001", 2 ** 22 + 12345),
            "in_progress": ("P-0001", os.getpid())}.get(case)
    if lock:
        with db.conn as c:
            c.execute("INSERT INTO run_lock (singleton, plan_id, acquired_at, pid) VALUES (1, ?, 'then', ?)", lock)
    if case == "in_progress":
        db.request_stop("P-0001")
    r = run("stop", "P-0001", "--target", str(plan3.root))
    why = {"not_executing": "only an executing plan", "other_plan": "P-0042 is running", "stale_lock": "remove the lock by hand",
           "in_progress": "a stop is already in progress"}[case]
    assert r.exit_code == 4 and why in r.output, r.output


def _status_paused(p, findings=None, note=None, conflict=False):
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans
    db = _executing(p)
    set_plan_status(p.paths, db, load_plans(p.paths.plans)["P-0001"], ["paused"], by="t", why="budget_exhausted: C-0001 used all")
    pid = db.record_pause("P-0001", "budget_exhausted", "C-0001 used all 2 BUILD attempts",
                          [_entry("C-0001", db.contract_hash("C-0001"))], conflict=conflict)
    db.set_pause_findings(pid, "C-0001", 7, findings, note)
    return db


def test_status_shows_findings_and_resume_commands(plan3):
    _status_paused(plan3, findings={"summary": "the helper already exists", "failure_class": "implementation",
                                    "evidence": [{"ref": "src/a/x.py:3", "shows": "helper()"}], "needs_owner_decision": None,
                                    "suggested_instruction": "use helper() from src/a/x.py"})
    r = run("status", "P-0001", "--target", str(plan3.root))
    out = r.output
    assert r.exit_code == 0 and "paused (budget_exhausted)" in out and "C-0001 used all 2 BUILD attempts" in out
    for text in ("the helper already exists", "implementation", "src/a/x.py:3", "use helper() from src/a/x.py",
                 "worktrees/C-0001", "ateam resume P-0001 --adopt", 'ateam resume P-0001 --note "',
                 "# resume with no instruction"):
        assert text in out, text
    assert "  P-0001 paused (budget_exhausted): ateam status P-0001" in run("status", "--target", str(plan3.root)).output


def test_status_shows_why_there_are_no_findings(plan3):
    _status_paused(plan3, note="tamper: refs changed (a branch or tag was created, moved or deleted)")
    out = run("status", "P-0001", "--target", str(plan3.root)).output
    assert "no findings: tamper: refs changed" in out and "ateam resume P-0001 --adopt" not in out


@pytest.mark.parametrize("case", ["current", "no_suggestion", "stale_hash", "owner_stopped"])
def test_status_offers_adopt_only_when_every_suggestion_is_current(case, plan3):
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans
    db = _executing(plan3)
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["paused"], by="t", why="budget_exhausted: x")
    if case == "owner_stopped":
        db.record_pause("P-0001", "owner_stopped", "stopped by the owner", [])
    else:
        h = "an-older-hash" if case == "stale_hash" else db.contract_hash("C-0001")
        pid = db.record_pause("P-0001", "budget_exhausted", "d", [_entry("C-0001", h)])
        findings = {"summary": "s"} if case == "no_suggestion" else {"summary": "s", "suggested_instruction": "use x"}
        db.set_pause_findings(pid, "C-0001", 7, findings, None)
    out = run("status", "P-0001", "--target", str(plan3.root)).output
    assert ("ateam resume P-0001 --adopt" in out) == (case == "current"), out


@pytest.mark.parametrize("paused", [["C-0001"], ["C-0001", "C-0003"]])
def test_status_note_names_contract_when_several_are_paused(paused, plan3):
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans
    db = _executing(plan3)
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["paused"], by="t", why="budget_exhausted: x")
    db.record_pause("P-0001", "budget_exhausted", "d", [_entry(c, db.contract_hash(c)) for c in paused])
    out = run("status", "P-0001", "--target", str(plan3.root)).output
    notes = [line.strip() for line in out.splitlines() if line.strip().startswith('ateam resume P-0001 --note "')]
    if len(paused) == 1:
        assert len(notes) == 1 and "--contract" not in notes[0], out
    else:
        assert len(notes) == 2 and all(f"--contract {c}" in n for n, c in zip(notes, paused)), out


@pytest.mark.parametrize("case", ["blank_note", "blank_note_file", "integrated_contract"])
def test_resume_refuses_blank_notes_and_integrated_contracts(case, plan3, monkeypatch, tmp_path):
    db = _paused(plan3, monkeypatch)
    args: tuple[str, ...] = ("--note", "   ", "--no-run")
    why = "the note is blank"
    if case == "blank_note_file":
        blank = tmp_path / "blank.txt"
        blank.write_text("  \n")
        args, why = ("--note-file", str(blank), "--no-run"), "the note file is blank"
    elif case == "integrated_contract":
        with db.conn as c:
            c.execute("UPDATE contracts SET status='integrated' WHERE contract_id='C-0001'")
        args, why = ("--note", "x", "--contract", "C-0001", "--no-run"), "C-0001 is integrated"
    status, head = db.feature_status("P-0001"), _intent_head(plan3)
    windows = [db.window_start(c) for c in ("C-0001", "C-0002", "C-0003")]
    r = _resume(plan3, *args)
    assert r.exit_code == 4 and why in r.output, r.output
    assert _sdb(plan3).feature_status("P-0001") == status == "paused" and _intent_head(plan3) == head
    assert [_sdb(plan3).window_start(c) for c in ("C-0001", "C-0002", "C-0003")] == windows
    assert GitRepo(plan3.paths.intent).status_porcelain() == []


def test_resume_message_distinguishes_no_record_from_an_empty_pause(plan3, monkeypatch):
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans
    db = _sdb(plan3)
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["executing", "paused"], by="t", why="budget_exhausted: old")
    r = _resume(plan3, "--no-run")
    assert r.exit_code == 0 and "no pause record; no contract's attempts were refreshed" in r.output, r.output
    assert "(the pause names no contract)" not in r.output
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["paused"], by="t", why="owner_stopped: stopped by the owner")
    db.record_pause("P-0001", "owner_stopped", "stopped by the owner", [])
    r = _resume(plan3, "--no-run")
    assert r.exit_code == 0 and "no contract's attempts were refreshed (the pause names no contract)" in r.output, r.output


def test_status_of_a_conflict_pause_names_the_rebase(plan3):
    _status_paused(plan3, conflict=True)
    out = run("status", "P-0001", "--target", str(plan3.root)).output
    assert "resume runs the rebase BUILD for C-0001" in out and "ateam resume P-0001" in out


def test_status_of_a_target_conflict_pause_refuses_resume(plan3):
    from ateam.plans.ops import set_plan_status
    from ateam.plans.store import load_plans
    db = _executing(plan3)
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["paused"], by="t", why="environment_failure: main conflicts")
    db.record_pause("P-0001", "environment_failure", "main conflicts in src/a/a.py", [], conflict=True)
    out = run("status", "P-0001", "--target", str(plan3.root)).output
    assert "resume cannot rebuild it" in out and "ateam resume P-0001" not in out


def test_resume_after_a_hook_refused_commit_resumes_again(plan3, monkeypatch):
    """Review Focus 1 in its realistic form: the commit fails after staging; the printed restore commands work."""
    import subprocess

    from ateam.guidance import load_guidance
    db = _paused(plan3, monkeypatch)
    gate = plan3.paths.intent.parent / "refuse-commits"
    gate.write_text("on\n")
    hook = plan3.paths.intent / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nif [ -e '{gate}' ]; then echo refused by hook >&2; exit 1; fi\n"); hook.chmod(0o755)
    r = _resume(plan3, "--note", "use x", "--no-run")
    assert r.exit_code == 1 and "refused by hook" in r.output, r.output
    for line in r.output.splitlines():
        if line.strip().startswith("git -C"):
            subprocess.run(line.strip(), shell=True, check=True)
    assert GitRepo(plan3.paths.intent).is_clean() and db.feature_status("P-0001") == "paused"
    gate.unlink()
    r2 = _resume(plan3, "--note", "use x", "--no-run")
    assert r2.exit_code == 0, r2.output
    assert [e.text for e in load_guidance(plan3.paths.guidance, "C-0001")] == ["use x"]


# ---- resume after a merge conflict (2026-10-27-lanes-design.md, rulings 8, 9 and 13)

def _conflict_paused(p, monkeypatch, script):
    from test_scheduler import _conflict_during  # noqa: F401  (callers build the script with it)
    _set_config(p.paths, checks=[NamedCheck(name="unit", cmd="true")], max_workers=1)
    fake = FakeWorkers(script)
    _fake(monkeypatch, fake)
    r = run("run", "P-0001", "--target", str(p.root))
    assert r.exit_code == 1 and "P-0001 -> paused (environment_failure: C-0001" in r.output, r.output
    assert _sdb(p).open_pause("P-0001")["conflict"] is True
    return fake


def test_resume_of_a_conflict_pause_runs_the_rebase_build(plan3, monkeypatch):
    from test_scheduler import _conflict_during, _rebase_builds
    _conflict_paused(plan3, monkeypatch, {("C-0001", "build", 0): _conflict_during(plan3),
                                          ("C-0001", "build", 1): _conflict_during(plan3, text="A = 'someone else again'\n")})
    fake = FakeWorkers()
    _fake(monkeypatch, fake)
    r = _resume(plan3)
    assert r.exit_code == 0 and "merge_proposed" in r.output, r.output
    assert len(_rebase_builds(fake, "C-0001")) == 1


def test_a_conflict_with_no_attempt_left_resumes_into_the_rebase(plan3, monkeypatch):
    from ateam.contracts.store import load_contracts
    from test_scheduler import _conflict_during, _rebase_builds
    allowed = load_contracts(plan3.paths.contracts)["C-0001"].budget.attempts
    script = {("C-0001", "build", n): {"code": 3, "outcome": "scope_breach", "commit": False} for n in range(allowed - 1)}
    script[("C-0001", "build", allowed - 1)] = _conflict_during(plan3)
    _conflict_paused(plan3, monkeypatch, script)
    fake = FakeWorkers()
    _fake(monkeypatch, fake)
    r = _resume(plan3)
    assert r.exit_code == 0 and "merge_proposed" in r.output, r.output
    assert len(_rebase_builds(fake, "C-0001")) == 1


def test_resume_after_the_tip_moved_keeps_the_open_merge(plan3, monkeypatch):
    import json

    from test_scheduler import _conflict_during, _land_on_integration, _rebase_builds
    first = _conflict_paused(plan3, monkeypatch, {("C-0001", "build", 0): _conflict_during(plan3), ("C-0001", "build", 1): {"commit": False}})
    tip = first.tips[0]
    assert json.loads(_sdb(plan3).get_meta("rebase:C-0001"))["tip"] == tip
    _land_on_integration(plan3, "docs/unrelated.md", "another lane\n")   # the tip moves on without touching C-0001's paths
    fake = FakeWorkers()
    _fake(monkeypatch, fake)
    r = _resume(plan3)
    assert r.exit_code == 0 and "merge_proposed" in r.output, r.output
    assert json.loads(_sdb(plan3).get_meta("rebase:C-0001"))["tip"] == tip   # the open merge was kept, not restarted
    assert len(_rebase_builds(fake, "C-0001")) == 1


def test_status_spend_counts_unknown_at_cap(plan3):
    with _sdb(plan3) as db:
        known = db.start_attempt("C-0001", "build", "light", "haiku", {})
        db.finish_attempt(known, "completed", None, 1, 1, 0.25, 3, 0.1, "s")
        db.interrupt_attempt(db.start_attempt("C-0002", "test", "standard", "sonnet", {}))   # NULL cost: the $2 cap
    r = run("status", "P-0001", "--target", str(plan3.root))
    assert r.exit_code == 0, r.output
    assert "spend: $2.25 over 2 attempt(s), 1 counted at its tier cap (cost unknown)" in r.output, r.output


# ---- resume and re-run restart after the last step that passed (C-0094)

def _two_tests_pass_then_crash(p, monkeypatch):
    """C-0001's and C-0003's TESTs pass side by side; the process dies at the first verify, before either is verified."""
    from ateam.scheduler import run_child
    from test_scheduler import _meet, _run

    def crash(argv, result_file):
        if "verify" in argv:
            raise SystemExit("killed at verify")
        return run_child(argv, result_file)

    with pytest.raises(SystemExit):   # plan3 runs two at once
        _run(p, FakeWorkers(_meet(("C-0001", "test"), ("C-0003", "test"))), child=crash)
    db = _sdb(p)
    assert all([r["passed"] for r in db.step_results(c)] == [True] for c in ("C-0001", "C-0003"))
    assert db.contract_status("C-0001") == db.contract_status("C-0003") == "ready"


def test_resume_note_forces_build_after_crash(plan3, monkeypatch):
    _two_tests_pass_then_crash(plan3, monkeypatch)
    assert run("stop", "P-0001", "--target", str(plan3.root)).exit_code == 0   # paused, so it can be resumed

    def dies(*a, **k):
        raise SystemExit(137)   # resume committed the guidance, then died before the run started

    import ateam.cli
    real = ateam.cli._run_foreground
    monkeypatch.setattr("ateam.cli._run_foreground", dies)
    r = _resume(plan3, "--note", "use x", "--contract", "C-0001")
    assert r.exit_code == 137 and "C-0001: instruction recorded" in r.output, r.output
    assert _sdb(plan3).feature_status("P-0001") == "executing"
    monkeypatch.setattr("ateam.cli._run_foreground", real)
    from test_scheduler import _rebuild
    fake = FakeWorkers({("C-0001", "build"): _rebuild})
    _fake(monkeypatch, fake)
    r = run("run", "P-0001", "--target", str(plan3.root))
    assert r.exit_code == 0 and "P-0001 -> merge_proposed" in r.output, r.output
    assert [c[1] for c in fake.calls if c[0] == "C-0001"] == ["build", "test"]
    assert [c for c in fake.calls if c[0] == "C-0003"] == []   # its pass still stands
    assert "C-0001 restarts at build: new guidance" in r.output and "C-0003 restarts at verify: TEST passed on" in r.output


def test_status_shows_restart_point(plan3, monkeypatch):
    from test_scheduler import _verify_crashes, _wt
    _verify_crashes(monkeypatch, plan3, cid="C-0003")
    out = run("status", "P-0001", "--target", str(plan3.root)).output
    lines = out.splitlines()
    assert f"    restarts at verify: TEST passed on {_wt(plan3, 'C-0003').head()[:8]}" in lines, out
    assert "    restarts at build: no worktree yet" in lines, out          # C-0002: not started
    assert sum("restarts at" in line for line in lines) == 2, out             # none for the integrated C-0001
    (_wt(plan3, "C-0003").root / "src" / "c" / "stray.py").write_text("X = 1\n")
    out = run("status", "P-0001", "--target", str(plan3.root)).output
    assert "    restarts at build: the worktree has uncommitted changes" in out.splitlines(), out
