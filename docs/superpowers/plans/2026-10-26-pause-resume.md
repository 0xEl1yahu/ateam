# Pause, investigate and resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A paused plan has a way out: `ateam stop` pauses a run, an automatic read-only INVESTIGATE session explains an ATeam pause and suggests an instruction, `ateam status P-xxxx` shows it, and `ateam resume` continues with refreshed attempts and the owner's guidance.

**Architecture:**
- **Store.** It records pauses (plan, reason, paused contracts, evidence, findings), attempt windows and stop requests. Contract statuses never change on a pause.
- **Scheduler.** It writes the pause record, counts BUILD attempts only inside the current window, turns an owner stop into `paused (owner_stopped)`, and launches one pause investigation per paused contract. Each investigation is a child `ateam worker pause-investigate`, injected in tests.
- **Guidance.** Instructions are YAML files in the intent repo (`guidance/C-xxxx.yaml`). BUILD briefs carry the live ones, meaning those whose contract hash matches.
- **CLI.** Three commands: `ateam stop`, `ateam resume` and `ateam status [PLAN_ID]`.

**Tech Stack:** Python 3.14, click, SQLite (`StateDB`), PyYAML/pydantic, git CLI, `subprocess`, `threading`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-pause-resume-design.md` (approved by the owner 2026-10-26); binding background `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`.

## Global Constraints

- **Prerequisite:** the plan/run exclusion change (owner, 2026-10-26) is merged first. It provides `require_no_live_planning(ctx: Context) -> None` in `src/ateam/cli.py` (refuses with exit 4 while an `ateam plan` session is live). `resume` and `stop` call it.
- **Pause reasons:** the spec enum plus `owner_stopped`: decision_change_needed, scope_exceeded, budget_exhausted, verification_unresolvable, sensitive_action, environment_failure, records_drifted, owner_stopped.
- **A pause changes no contract status and makes no contract-file commit.** The plan file goes `executing|integrating -> paused` as today.
- **Intent first, store second.** The one exception is attempt-window rows, which resume writes before its intent commit. They are idempotent: a repeated resume opens a newer window with the same effect.
- **Instructions never reach TEST.** They reach BUILD only when the entry's `contract_hash` equals the contract's current `content_hash()`.
- **Pause investigations:**
  - role INVESTIGATE, tier `careful`, read-only, cwd the contract worktree `<home>/worktrees/<cid>`;
  - the full `runner/snapshot.take` snapshot plus the intent repo check, never narrowed; any change means `tamper`, the findings are discarded and the pause stands;
  - never for `owner_stopped`, and never for a pause with no contract;
  - scratch directory `<home>/scratch/<cid>-investigate`.
- **Budget:**
  - the attempt limit is the frozen `budget.attempts`, counted over BUILD attempts with `attempt_id > window_start(cid)`, interrupted ones excluded;
  - escalation is judged inside the same window;
  - an investigation reserves `tiers["careful"][Role.investigate].usd` against `run_budget_usd`.
- **`ateam stop`:**
  - records `stop-request:<plan>` in the store, then sends SIGINT to the run lock holder's process group (`os.killpg(os.getpgid(pid), SIGINT)`);
  - with no live holder it pauses the plan directly;
  - refuses a plan that is not `executing`, and a second stop while one is in progress.
- **Exit codes:** `stop` 0 (requested or paused) or 4 (refused). `resume` 4 when refused; otherwise `ateam run`'s codes (0, 1, 4, 130), or 0 with `--no-run`.
- **Unchanged rules:**
  - git-native only and no `rm`;
  - nothing pushed or fetched;
  - approval gates unchanged;
  - a paused plan is still refused by `ateam run`, which now names `ateam resume`;
  - Ctrl-C in the run's terminal keeps its meaning: the run ends `interrupted` and the plan stays `executing`, unless a stop request is recorded.
- **Tests:**
  - real git on scratch targets;
  - scheduler tests inject `launch=` and `investigate=` and never start a real session;
  - in `cli_target`, C-0001 is the seed fixture;
  - never `monkeypatch.undo()`.
- **Commits** end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **A resume interrupted between its store windows and its intent commit.** Running `ateam resume` again succeeds, opens a window and does not double-count anything (Task 7).
2. **A plan paused before this change, with no pause record.** Resume works, and opens a window for every unfinished contract, so a `budget_exhausted` contract does not pause again at once (Task 7).
3. **`ateam stop` while the scheduler is inside an exclusive step (verify or merge).** The step finishes, nothing is marked `tamper`, and the plan ends `paused (owner_stopped)` (Task 4).
4. **The owner commits in their checkout while a pause investigation runs.** The findings are discarded as tamper, `status` says why, and the pause still resumes (Tasks 5 and 8).
5. **A contract re-frozen between pause and resume.** Old instructions are not delivered, and the window still opens (Tasks 3 and 7).

---

### Task 1: Store: pauses, attempt windows, stop requests

**Files:**
- Modify: `src/ateam/state/db.py`. Add three tables to `_SCHEMA` and the methods below.
- Test: `tests/test_state_db.py`

**Interfaces:**
- Produces, all on `StateDB`:
  - `record_pause(plan_id: str, reason: str, detail: str, contracts: list[dict], conflict: bool = False) -> int` returns `pause_id`. Each contract dict has `contract_id`, `reason`, `detail`, `contract_hash` and `evidence` (`dict | None`).
  - `open_pause(plan_id: str) -> dict | None` returns the latest pause with `resumed_at IS NULL`. Keys: `pause_id`, `plan_id`, `reason`, `detail`, `conflict` (bool), `at`, and `contracts` (a list of dicts with the fields above plus `attempt_id`, `findings` (`dict | None`) and `note`).
  - `set_pause_findings(pause_id: int, contract_id: str, attempt_id: int | None, findings: dict | None, note: str | None) -> None`.
  - `resume_pause(pause_id: int | None, contract_ids: list[str]) -> None`. One transaction: for each contract id it inserts a window with `after_attempt_id = MAX(attempt_id)` over all attempts (0 when there are none), and sets `resumed_at` when `pause_id` is given.
  - `window_start(contract_id: str) -> int` returns `after_attempt_id` of the contract's newest window, or 0.
  - `request_stop(plan_id: str) -> None`, `stop_requested(plan_id: str) -> bool` and `clear_stop(plan_id: str) -> None`, over meta key `stop-request:<plan_id>`.
- Tables:
  - `pauses(pause_id PK AUTOINCREMENT, plan_id, reason, detail, conflict INTEGER, at, resumed_at)`.
  - `pause_contracts(pause_id, contract_id, reason, detail, contract_hash, evidence TEXT, attempt_id INTEGER, findings TEXT, note TEXT, PRIMARY KEY (pause_id, contract_id))`.
  - `attempt_windows(window_id PK AUTOINCREMENT, contract_id, after_attempt_id INTEGER NOT NULL, pause_id INTEGER, at)`.
  - `CREATE TABLE IF NOT EXISTS`, so an existing `state.db` gains them on `init_schema`.

- [ ] **Step 1: Write the failing tests**

```python
def test_pause_record_round_trip(tmp_path):
    db = fresh(tmp_path)
    pid = db.record_pause("P-0001", "budget_exhausted", "C-0002 has used all 2 BUILD attempts",
                          [{"contract_id": "C-0002", "reason": "budget_exhausted", "detail": "d", "contract_hash": "h1",
                            "evidence": {"code": 1}}])
    p = db.open_pause("P-0001")
    assert p["pause_id"] == pid and p["conflict"] is False and p["contracts"][0]["evidence"] == {"code": 1}
    db.set_pause_findings(pid, "C-0002", 7, {"summary": "s"}, None)
    assert db.open_pause("P-0001")["contracts"][0]["findings"] == {"summary": "s"}
    db.resume_pause(pid, ["C-0002"])
    assert db.open_pause("P-0001") is None


def test_window_starts_after_the_newest_attempt(tmp_path):
    db = fresh(tmp_path)
    assert db.window_start("C-0002") == 0
    a = db.start_attempt("C-0002", "build", "light", "haiku", {})
    db.start_attempt("C-0003", "build", "light", "haiku", {})
    db.resume_pause(None, ["C-0002"])
    assert db.window_start("C-0002") == a + 1 and db.window_start("C-0003") == 0


def test_stop_request_flag(tmp_path):
    db = fresh(tmp_path)
    assert not db.stop_requested("P-0001")
    db.request_stop("P-0001"); assert db.stop_requested("P-0001")
    db.clear_stop("P-0001"); assert not db.stop_requested("P-0001")


def test_existing_store_gains_the_pause_tables(tmp_path):
    db = fresh(tmp_path)
    with db.conn:
        db.conn.execute("DROP TABLE pauses"); db.conn.execute("DROP TABLE pause_contracts"); db.conn.execute("DROP TABLE attempt_windows")
    db.init_schema()
    assert db.open_pause("P-0001") is None and db.window_start("C-0001") == 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/pytest -q tests/test_state_db.py -k "pause or window or stop_request"`
Expected: FAIL with `AttributeError: 'StateDB' object has no attribute 'record_pause'`

- [ ] **Step 3: Implement the tables and methods in `src/ateam/state/db.py`.** JSON columns use `json.dumps(..., default=str)` and `json.loads` on read; `conflict` is stored as 0/1 and returned as bool.

- [ ] **Step 4: Run the file**

Run: `.venv/bin/pytest -q tests/test_state_db.py`
Expected: all pass.

- [ ] **Step 5: Commit** `feat(state): pause records, attempt windows and stop requests`

---

### Task 2: Pause reasons tell the truth (#17), plus `owner_stopped`

**Files:**
- Modify: `src/ateam/retry.py` (PAUSE_REASONS, StepOutcome, classify, `_PAUSE_NOW`); `src/ateam/cli.py` `verify` (a `refusal` result field); `src/ateam/scheduler.py` (pass `refusal` into `StepOutcome`).
- Test: `tests/test_retry.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `PAUSE_REASONS` includes `"owner_stopped"`.
  - `StepOutcome.refusal: str | None = None`.
  - The verify result file gains `"refusal"`: `"not_binding"` for a `ContractNotBinding`, `"intent_dirty"` for an `IntentError`, `"other"` for any other refusal, and `null` when the command did not refuse.
  - `classify` for a verify with status `refused` returns `"not_binding"` when `refusal == "not_binding"` and `"refused"` otherwise.
  - `_PAUSE_NOW["refused"] = "environment_failure"`; a worker run's `refused` outcome is the run's own environment failing too.
  - `requirements` stays `decision_change_needed`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_retry.py
@pytest.mark.parametrize("refusal,reason", [("not_binding", "records_drifted"), ("intent_dirty", "environment_failure"),
                                            ("other", "environment_failure"), (None, "environment_failure")])
def test_verify_refusal_pauses_with_its_cause(refusal, reason):
    o = StepOutcome("verify", 4, "refused", verify_status="refused", refusal=refusal)
    assert decide(classify(o), 0, 2, "light", False, None, stage="verify", stage_used=1) == Decision("pause", "light", reason)


def test_owner_stopped_is_a_pause_reason():
    assert "owner_stopped" in PAUSE_REASONS


def test_requirements_still_needs_a_decision():
    o = StepOutcome("test", 5, "failed", test_class="requirements")
    assert decide(classify(o), 1, 2, "light", False, None, stage="test", stage_used=1).reason == "decision_change_needed"
```

```python
# tests/test_cli.py: extend test_verify_result_file_marks_refusals_and_verdicts' style
def test_verify_result_file_names_the_refusal(cli_target, tmp_path):
    out = tmp_path / "v.json"
    wt = _verify_setup(cli_target, tmp_path, [("unit", "true")])
    _do_work(wt)
    (TargetPaths.from_root(cli_target).intent / "stray.txt").write_text("x\n")       # intent repo dirty
    r = run("verify", "C-0001", "--cwd", str(wt), "--result-file", str(out), "--target", str(cli_target))
    assert r.exit_code == 4 and _read(out)["status"] == "refused" and _read(out)["refusal"] == "intent_dirty"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/pytest -q tests/test_retry.py tests/test_cli.py -k "refusal or owner_stopped or requirements_still"`
Expected: FAIL. `StepOutcome` has no `refusal`, and the verify result has no `refusal` key.

- [ ] **Step 3: Implement it.**
  - In `verify`, add `"refusal": None` to the `_Result` defaults and set it in the existing `except (ContractNotBinding, IntentError)` branch by exception type.
  - The `_Result` exit path sets `"other"` when a refusal left it `None` and the status is `refused`.
  - The scheduler's verify `StepOutcome(...)` passes `refusal=v.get("refusal")`.

- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_retry.py tests/test_cli.py tests/test_scheduler.py`. Expected: all pass. A scheduler test that asserted `decision_change_needed` for a refusal must be updated to the new reason. Say so in the commit message.

- [ ] **Step 5: Commit** `fix(retry): verify refusals pause with their cause; owner_stopped reason`

---

### Task 3: Guidance: storage and delivery to BUILD

**Files:**
- Create: `src/ateam/guidance.py`
- Modify: `src/ateam/paths.py` (property), `src/ateam/brief.py` (section), `src/ateam/runner/run.py` (load for BUILD); the `brief` CLI command in `src/ateam/cli.py` passes the same instructions.
- Test: `tests/test_guidance.py` (new), `tests/test_brief.py`, `tests/test_run.py`

**Interfaces:**
- Produces:
  - `TargetPaths.guidance` property returning `self.intent / "guidance"`.
  - In `ateam.guidance`:
    - `class GuidanceEntry(BaseModel)` with `at: datetime`, `source: str` (`"owner"` or `"A-<attempt_id>"`), `contract_hash: str` and `text: str`;
    - `load_guidance(directory: Path, contract_id: str) -> list[GuidanceEntry]`, which returns `[]` when the file is missing;
    - `append_guidance(directory: Path, contract_id: str, entry: GuidanceEntry) -> Path`, which writes `<directory>/<contract_id>.yaml` as `{contract: <id>, entries: [...]}` and returns the path;
    - `live_guidance(entries: list[GuidanceEntry], contract_hash: str) -> list[GuidanceEntry]`.
  - `Brief.instructions: str | None = None`.
  - `assemble_brief(..., instructions: list[GuidanceEntry] = ())` renders them only when `role is Role.build`.
  - `render_brief` places `## Owner's instructions` after "Verified prior work" and before "Previous worker's notes (unverified)".
- Section text (exact):

```
## Owner's instructions
Guidance from the owner for meeting this contract. It does not change the contract or any binding record; if it ever seems to, the contract and records win.
- <YYYY-MM-DD>, owner: <text>
- <YYYY-MM-DD>, adopted from investigation <source>: <text>
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guidance.py
def test_append_load_and_live(tmp_path):
    e1 = GuidanceEntry(at=datetime(2026, 10, 27, tzinfo=UTC), source="owner", contract_hash="h1", text="use the helper in x.py")
    e2 = GuidanceEntry(at=datetime(2026, 10, 28, tzinfo=UTC), source="A-42", contract_hash="h2", text="later")
    append_guidance(tmp_path, "C-0002", e1); p = append_guidance(tmp_path, "C-0002", e2)
    assert p == tmp_path / "C-0002.yaml" and load_guidance(tmp_path, "C-0002") == [e1, e2]
    assert live_guidance(load_guidance(tmp_path, "C-0002"), "h2") == [e2]
    assert load_guidance(tmp_path, "C-0009") == []
```

```python
# tests/test_brief.py
def test_owner_instructions_reach_build_only(contract, records):
    e = GuidanceEntry(at=datetime(2026, 10, 27, tzinfo=UTC), source="A-42", contract_hash="h", text="use the helper")
    build = render_brief(assemble_brief(contract, records, None, Role.build, instructions=[e]))
    assert "## Owner's instructions" in build and "- 2026-10-27, adopted from investigation A-42: use the helper" in build
    assert build.index("## Verified prior work") < build.index("## Owner's instructions")
    assert "Owner's instructions" not in render_brief(assemble_brief(contract, records, None, Role.test, instructions=[e]))
```

```python
# tests/test_run.py: run_contract_attempt with a captured brief (reuse the file's existing capture helper)
def test_build_brief_carries_only_live_guidance(...):
    # entries for the contract's current content_hash() and for "stale-hash"; BUILD brief has the first, not the second
```

- [ ] **Step 2: Run them and watch them fail.** `.venv/bin/pytest -q tests/test_guidance.py tests/test_brief.py tests/test_run.py -k "guidance or instructions"`. Expected: an ImportError for `ateam.guidance`.

- [ ] **Step 3: Implement it.** `run_contract_attempt` passes `live_guidance(load_guidance(paths.guidance, contract.id), contract.content_hash())` for `Role.build` and `()` otherwise.

- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_guidance.py tests/test_brief.py tests/test_run.py tests/test_cli.py -k "brief or guidance or run"`. Expected: all pass.

- [ ] **Step 5: Commit** `feat(guidance): owner instructions in the intent repo reach BUILD briefs`

---

### Task 4: Scheduler: pause records, attempt windows, owner stop

**Files:**
- Modify: `src/ateam/scheduler.py`, `src/ateam/integrate.py` (`_pause` records a pause).
- Test: `tests/test_scheduler.py`, `tests/test_integrate.py`

**Interfaces:**
- Consumes: Task 1's `record_pause`, `window_start`, `stop_requested` and `clear_stop`.
- Produces:
  - `_Outcome` gains `evidence: dict | None = None` (the result dict of the step that paused: the last `r` or `v`) and `conflict: bool = False` (True for the `MergeConflict` pause in `_merge`).
  - On pause, `_Run.execute` sets the plan paused (as today), then calls `db.record_pause(plan_id, first.reason, first.detail, [one dict per paused outcome], conflict=any(o.conflict for o in pauses))`, with `contract_hash = db.contract_hash(cid)`.
  - `_counted_builds` keeps only attempts with `attempt_id > db.window_start(cid)`.
  - Owner stop: when `self.stop` is set and `db.stop_requested(plan_id)`, `execute` closes open attempts, sets the plan paused with why `"owner_stopped: stopped by the owner"`, calls `record_pause(plan_id, "owner_stopped", "stopped by the owner", [])` and `clear_stop(plan_id)`, and returns outcome `"paused"`.
  - `run_plan` clears a leftover stop request after it takes the lock.
  - `_preflight`'s paused refusal reads `"{plan_id} is paused ({reason}); resume it with: ateam resume {plan_id}"`.
  - `integrate._pause(...)` calls `db.record_pause(plan.id, reason, detail, [], conflict=(kind == "conflict"))`. The reason and detail come from splitting `why` at the first `": "`.

- [ ] **Step 1: Write the failing tests** (the `plan3` and `FakeWorkers` helpers already in the file)

```python
def test_a_pause_is_recorded_with_its_contract_and_evidence(plan3):
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}})
    s, out = _run(plan3, fake)
    p = _db(plan3).open_pause("P-0001")
    assert s.outcome == "paused" and p["reason"] == "decision_change_needed" and not p["conflict"]
    [c] = p["contracts"]
    assert c["contract_id"] == "C-0001" and c["evidence"]["test_class"] == "requirements"
    assert c["contract_hash"] == _db(plan3).contract_hash("C-0001")
    assert _db(plan3).contract_status("C-0001") == "ready"          # no contract status change


def test_merge_conflict_pause_is_marked_conflict(plan3):
    # reuse conflicting_merge_lands_first from test_merge_conflict_pauses_environment_failure_contract_verified
    ...
    assert _db(plan3).open_pause("P-0001")["conflict"] is True


def test_a_window_refreshes_attempts_and_escalation(plan3):
    turns = {"code": 1, "outcome": "failed", "subtype": "error_max_turns", "commit": False}
    fake = FakeWorkers({("C-0001", "build"): turns})
    _run(plan3, fake)                                              # escalates once, then budget_exhausted
    db = _db(plan3); pause = db.open_pause("P-0001")
    used = len([a for a in db.attempts_for("C-0001", role="build")])
    db.resume_pause(pause["pause_id"], ["C-0001"])
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["executing"], by="t")   # file and store
    fake2 = FakeWorkers()
    s, out = _run(plan3, fake2)
    assert s.outcome == "merge_proposed" and fake2.tiers("C-0001")[0] == "light"   # frozen tier again
    assert len(_db(plan3).attempts_for("C-0001", role="build")) == used + 1


def test_owner_stop_pauses_owner_stopped(plan3):
    stop = threading.Event()

    def owner_stop(fake, cid, role, worktree, n):
        _db(plan3).request_stop("P-0001"); stop.set()
        return {"code": 130, "outcome": "interrupted", "commit": False}

    s, out = _run(plan3, FakeWorkers({("C-0001", "build", 0): owner_stop}), stop=stop)
    db = _db(plan3)
    assert s.outcome == "paused" and _last_reason(plan3) == "owner_stopped: stopped by the owner"
    assert db.open_pause("P-0001")["contracts"] == [] and not db.stop_requested("P-0001") and db.open_attempts(list(NAMES)) == []


def test_owner_stop_during_an_exclusive_step_finishes_it_without_tamper(plan3):
    # Review Focus 3: request the stop from inside the verify child (wrap `child`), assert the verify recorded its
    # verdict, no attempt has outcome "tamper", and the plan ends paused (owner_stopped)


def test_paused_plan_refusal_names_resume(plan3):
    # pause it (requirements fake), then _run again: RunRefused message contains "ateam resume P-0001"
```

```python
# tests/test_integrate.py
def test_integration_conflict_records_a_conflict_pause(...):
    # an existing conflict fixture in this file; afterwards db.open_pause(plan)["conflict"] is True and contracts == []
```

- [ ] **Step 2: Run them and watch them fail.** Run: `.venv/bin/pytest -q tests/test_scheduler.py tests/test_integrate.py -k "pause or window or owner_stop or names_resume"`

- [ ] **Step 3: Implement it.** The stop request is checked at the existing `if self.stop.is_set():` branch after the loop. A second Ctrl-C still exits at once.

- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_scheduler.py tests/test_integrate.py tests/test_retry.py` three times. Expected: all pass each time.

- [ ] **Step 5: Commit** `feat(scheduler): pause records, attempt windows and owner stop`

---

### Task 5: The pause investigation session

**Files:**
- Modify:
  - `src/ateam/roles.py`: `PAUSE_SCHEMA` and `PAUSE_INVESTIGATE_SPEC`.
  - `src/ateam/brief.py`: `assemble_pause_brief`.
  - `src/ateam/runner/investigate.py`: cwd and policy from `target_git.root`, plus `scratch_name`.
  - `src/ateam/cli.py`: the `worker pause-investigate` command.
- Test: `tests/test_roles.py`, `tests/test_brief.py`, `tests/test_investigate.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1's `open_pause` and `set_pause_findings`; Task 3's `load_guidance` and `live_guidance`.
- Produces:
  - `PAUSE_SCHEMA`: an object requiring `summary` (string), `evidence` (an array of `{ref: string, shows: string}`), `failure_class` (enum `implementation`, `requirements`, `environment`, `invariant_violation`), `needs_owner_decision` (string or null) and `suggested_instruction` (string or null).
  - `PAUSE_INVESTIGATE_SPEC = RoleSpec(role=Role.investigate, tools=_INVESTIGATE_TOOLS, ..., report_schema=PAUSE_SCHEMA)`. Its system prompt states the role is read-only and diagnostic, and that `suggested_instruction` must be guidance within the frozen contract and never a change to it.
  - `assemble_pause_brief(contract, records: list[Record], paused: dict, attempts: list[dict], build_checkpoint: Checkpoint | None, instructions: list[GuidanceEntry]) -> str`. Sections, in order:
    - `## Binding`;
    - `## Task`;
    - `## Why the plan paused`: the reason and detail, plus the evidence JSON pretty-printed, capped at 12 000 characters;
    - `## Attempt history`: one line per attempt with role, tier, outcome, failure_class and cost;
    - `## Previous worker's notes and report (unverified)`;
    - `## Instructions already given`.
  - `run_investigation(..., scratch_name: str | None = None)`, where cwd and `Policy.worktree` become `target_git.root`. Existing callers pass the owner's checkout, so their behaviour is unchanged.
  - CLI `ateam worker pause-investigate PLAN_ID CONTRACT_ID --cwd PATH [--tier careful] --result-file PATH --target PATH`. It:
    - reads `open_pause(plan)` and that contract's entry;
    - refuses with exit 4, `"outcome": "refused"`, when there is no open pause naming the contract;
    - runs `run_investigation(name=cid, scratch_name=f"{cid}-investigate", target_git=GitRepo(cwd), spec=PAUSE_INVESTIGATE_SPEC, ...)`;
    - writes the findings with `set_pause_findings(pause_id, cid, attempt_id, report or None, note)`, where `note` is `"tamper: <first change>"` when tampered and `"no report"` when the report is missing;
    - result-file fields: `code`, `outcome` (`completed`, `tamper`, `failed`, `refused` or `interrupted`), `attempt_id`, `cost_usd` and `error`;
    - exit codes: 0 completed, 1 tamper or failed, 4 refused.

- [ ] **Step 1: Write the failing tests**
  - `test_pause_schema_requires_the_five_fields` (roles).
  - `test_pause_brief_sections_in_order` (brief): it asserts each heading appears in the order above, the evidence is capped, and the instructions section lists live guidance.
  - `test_investigation_runs_in_the_given_checkout` (investigate): a fake `run_worker` captures `options.cwd == target_git.root`.
  - `test_investigation_in_a_worktree_sees_a_shared_ref_change_as_tamper` (investigate; Review Focus 4): the fake worker creates a branch in the owner's checkout during the session, then asserts `tampered` names "refs changed" and `res.report is None`.
  - `test_pause_investigate_records_findings_and_refuses_without_a_pause` (cli): with `run_investigation` monkeypatched to return a canned report, it asserts the findings are stored and the result file says `completed`; with no open pause the exit is 4 and the outcome `refused`.

- [ ] **Step 2: Run them and watch them fail.** Run: `.venv/bin/pytest -q tests/test_roles.py tests/test_brief.py tests/test_investigate.py tests/test_cli.py -k "pause or given_checkout or shared_ref"`

- [ ] **Step 3: Implement it.** The command reads tiers with `load_tiers(ctx.config.tiers)["careful"][Role.investigate]` unless `--tier` is given.

- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_roles.py tests/test_brief.py tests/test_investigate.py tests/test_cli.py tests/test_policy.py`. Expected: all pass.

- [ ] **Step 5: Commit** `feat(investigate): pause investigation session in the contract worktree`

---

### Task 6: Scheduler launches pause investigations

**Files:**
- Modify: `src/ateam/scheduler.py`
- Test: `tests/test_scheduler.py` (and `_run` helper: `kw.setdefault("investigate", FakeInvestigator())`, so no existing test starts a real session)

**Interfaces:**
- Consumes: Task 4's pause record; Task 5's command and result-file fields.
- Produces:
  - `Investigate = Callable[[Path, str, str, Path, Path], dict]`, called as `(target, plan_id, cid, worktree, result_file)`.
  - `default_investigate(...)` runs `ateam worker pause-investigate` like `default_launch`, in this process group, so Ctrl-C reaches it.
  - `run_plan(..., investigate: Investigate = default_investigate)`.
  - After `record_pause`, and only for a reason other than `owner_stopped`, there is one call per paused contract, in contract-id order, sequentially:
    - with no worktree at `<home>/worktrees/<cid>`, it calls `set_pause_findings(pid, cid, None, None, "no worktree to investigate")`;
    - otherwise it reserves `tiers["careful"][Role.investigate].usd` through `Budget.reserve`; on `"exhausted"` it records the note `"run budget $X would be exceeded by the investigation"`;
    - otherwise it calls `investigate`, settles the cost and says `"{cid} investigate {outcome} ${cost}"`;
    - on an interrupt (stop set) it records the note `"interrupted"` and stops investigating.
  - The plan is paused before the first investigation starts.

- [ ] **Step 1: Write the failing tests**

```python
class FakeInvestigator:
    def __init__(self, report=None, outcome="completed"):
        self.calls, self.report, self.outcome = [], report or {"summary": "s", "evidence": [], "failure_class": "implementation",
                                                                   "needs_owner_decision": None, "suggested_instruction": "use x"}, outcome
    def __call__(self, target, plan_id, cid, worktree, result_file):
        db = StateDB(home_dir(target) / "state.db"); db.init_schema()
        self.calls.append((cid, db.feature_status(plan_id), worktree))
        pause = db.open_pause(plan_id)
        db.set_pause_findings(pause["pause_id"], cid, None, self.report if self.outcome == "completed" else None, None)
        r = {"code": 0, "outcome": self.outcome, "attempt_id": None, "cost_usd": 0.2, "error": None}
        result_file.write_text(json.dumps(r)); return r


def test_an_ateam_pause_investigates_each_paused_contract_once(plan3):
    inv = FakeInvestigator()
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}})
    s, out = _run(plan3, fake, investigate=inv)
    assert [c[0] for c in inv.calls] == ["C-0001"] and inv.calls[0][1] == "paused"
    assert inv.calls[0][2] == home_dir(plan3.root) / "worktrees" / "C-0001"
    assert _db(plan3).open_pause("P-0001")["contracts"][0]["findings"]["suggested_instruction"] == "use x"


def test_owner_stop_is_not_investigated(plan3):  # as test_owner_stop_pauses_owner_stopped, with inv; inv.calls == []


def test_investigation_that_does_not_fit_the_budget_leaves_a_note(plan3):
    # run_budget_usd set just above one BUILD+TEST+verify path; findings None, note starts "run budget"
```

- [ ] **Step 2: Run them and watch them fail.** `.venv/bin/pytest -q tests/test_scheduler.py -k "investigat"`

- [ ] **Step 3: Implement it.**

- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_scheduler.py` three times. Expected: all pass each time.

- [ ] **Step 5: Commit** `feat(scheduler): investigate each paused contract automatically`

---

### Task 7: `ateam resume` (and `ateam run`'s foreground loop shared)

**Files:**
- Modify: `src/ateam/cli.py`. Extract `_run_foreground(target: Path, plan_id: str, dry_run: bool) -> NoReturn` from `run_cmd`'s body; `run_cmd` calls it. Add `resume`. Extract the per-contract checks of `scheduler._preflight` into `scheduler.binding_problems(ctx, cids: list[str]) -> list[str]`; `_preflight` raises on the first problem.
- Test: `tests/test_run_command.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Task 1 (`open_pause`, `resume_pause`); Task 3 (`append_guidance`, `GuidanceEntry`, `TargetPaths.guidance`); `set_plan_status(..., extra_files=...)`; `require_no_live_planning(ctx)` (prerequisite).
- Produces: `ateam resume PLAN_ID [--adopt | --note TEXT | --note-file PATH] [--contract C-xxxx]... [--no-run] [--target]`. In order:
  1. Refuse (exit 4) unless the plan is `paused` in the store or its file; refuse while the run lock is held or a planning session is live.
  2. Refuse a pause whose record has `conflict` true: `"{plan} paused on a merge conflict ({detail}); resume is refused until the rebase BUILD exists"`.
  3. Refuse when `binding_problems` reports any problem for the plan's unfinished contracts (status not `integrated`), naming each.
  4. Work out which contracts the instruction is for. With `--contract`, those named, each of which must belong to the plan. Otherwise, the paused contracts of the record, if there is exactly one. `--note` with zero or several candidates and no `--contract` is refused. `--adopt` takes each candidate contract's own `findings.suggested_instruction` and refuses when any is missing. `--adopt` together with `--note` or `--note-file` is refused. Any instruction is refused when every unfinished contract is `integrated` (a pause during final integration, where resume re-runs integration).
  5. Pick the contracts that get a new window: the record's paused contracts. When there is no open pause record (a plan paused before this change), or the record lists none, every unfinished contract gets one.
  6. `db.resume_pause(None, window_cids)`. Windows first; they are idempotent.
  7. Append each instruction (`source="owner"` or `f"A-{attempt_id}"`, `contract_hash=contract.content_hash()`), then `set_plan_status(paths, db, plan, ["executing"], by="owner", why="resumed", extra_files=[guidance files])`. That is one intent commit, refused when the intent repo is dirty.
  8. `db.resume_pause(pause_id, [])` marks the record resumed.
  9. Unless `--no-run`, `_run_foreground(target, plan_id, False)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_run_command.py` reuses `tests/test_scheduler.py`'s `plan3`/`FakeWorkers` through a monkeypatched `scheduler.default_launch` and `default_investigate`)
  - `test_resume_refusals`, table-driven:
    - not paused;
    - run lock held;
    - conflict pause;
    - an unfinished contract hand-edited after freeze;
    - dirty intent repo;
    - `--adopt` with no suggestion;
    - `--adopt --note`;
    - `--note` with two paused contracts and no `--contract`.

    Each expects exit 4 and a message naming the cause; afterwards `feature_status` is still `paused`.
  - `test_resume_with_a_note_commits_guidance_and_runs_to_merge_proposed`: after a `budget_exhausted` pause, `ateam resume P-0001 --note "use x"` exits 0. The intent log has one commit touching `plans/` and `guidance/C-0001.yaml`. The next BUILD brief contains the note, and the run ends `merge_proposed`.
  - `test_resume_adopt_records_the_investigation_source`: the guidance entry's `source` is `A-<attempt_id>`.
  - `test_resume_no_run_only_changes_state`.
  - `test_resume_after_a_lost_intent_commit_resumes_again` (Review Focus 1): monkeypatch `set_plan_status` to raise once. A second `resume` succeeds, and `window_start` is the newer window.
  - `test_resume_of_a_plan_paused_without_a_record` (Review Focus 2): pause through a direct `set_plan_status(["paused"])` with no record. Resume opens windows for all unfinished contracts, and the run proceeds.
  - `test_refrozen_contract_gets_a_window_and_no_stale_guidance` (Review Focus 5): add guidance, re-freeze C-0001 with `--force` after a change, resume with no instruction, and assert the BUILD brief has no "Owner's instructions".

- [ ] **Step 2: Run them and watch them fail.** Run: `.venv/bin/pytest -q tests/test_run_command.py -k resume`

- [ ] **Step 3: Implement it.**

- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_run_command.py tests/test_scheduler.py tests/test_cli.py`. Expected: all pass.

- [ ] **Step 5: Commit** `feat(cli): ateam resume with owner guidance and refreshed attempts`

---

### Task 8: `ateam stop` and `ateam status [PLAN_ID]`

**Files:**
- Modify: `src/ateam/cli.py` (`stop`, `status`); reuse `_process_alive` (already in cli.py).
- Test: `tests/test_run_command.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 (`request_stop`, `stop_requested`, `record_pause`, `open_pause`); `set_plan_status`; `require_no_live_planning`.
- Produces:
  - `ateam stop PLAN_ID [--target]`:
    - refuses (exit 4) unless the plan is `executing`, and refuses when `stop_requested` is already set while the lock holder is alive (`"a stop is already in progress"`);
    - if the run lock names this plan and its pid is alive, it calls `request_stop` and then `os.killpg(os.getpgid(pid), signal.SIGINT)`, and prints `"stop requested; {plan} pauses after its running sessions are interrupted"`;
    - otherwise, guarded by `require_no_live_planning` because it commits, it sets the plan paused with why `"owner_stopped: stopped by the owner"`, records the pause with no contracts, and adds `"; the run lock names a process that is gone: remove it by hand"` when a stale lock exists. Exit 0.
  - `ateam status [PLAN_ID]`. Without an argument the output is unchanged, plus `"  {pid} paused ({reason}): ateam status {pid}"` for each paused plan. With a paused plan it prints:
    - the reason and detail;
    - per paused contract: its reason, worktree path, findings summary, failure class, evidence lines, `needs_owner_decision` and suggested instruction, or the `note` when there are no findings;
    - then the three resume commands, exactly as in the spec's "What the owner sees", with the plan id filled in.

    A conflict pause prints `"resume is refused until the rebase BUILD exists"` instead of the commands. A plan that isn't paused prints its status line and the contracts of that plan.

- [ ] **Step 1: Write the failing tests**
  - `test_stop_signals_the_live_run_group`: insert a lock row with this test's pid, monkeypatch `os.killpg` and `os.getpgid`, and assert `killpg` was called with `(getpgid(pid), SIGINT)` and `stop_requested` is set.
  - `test_stop_without_a_live_run_pauses_directly`: stale-lock and no-lock variants; the plan is paused `owner_stopped`, there is one intent commit, and the stale-lock message is shown.
  - `test_stop_refusals`: not executing, and stop already in progress.
  - `test_status_shows_findings_and_resume_commands`: a seeded pause record with findings, and one with a tamper note (Review Focus 4).
  - `test_status_for_a_conflict_pause_refuses_resume`.

- [ ] **Step 2: Run them and watch them fail.** Run: `.venv/bin/pytest -q tests/test_run_command.py tests/test_cli.py -k "stop or status"`

- [ ] **Step 3: Implement it.**

- [ ] **Step 4: Run** the full suite, `.venv/bin/pytest -q`. Expected: all pass, with no new skips.

- [ ] **Step 5: Commit** `feat(cli): ateam stop and the paused-plan status view`

---

### Task 9: Spec, CLAUDE.md, README, and the live check

**Files:**
- Modify:
  - `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`: the pause reasons enum, the decisions, the replaced deviation bullet, the amended carve-out and the CLI surface;
  - `CLAUDE.md`: rule 6, which now allows pause investigations in the contract worktree with the full snapshot;
  - `README.md` step 5, the command list and the "next stages" line;
  - `docs/first-run-walkthrough.md` ("what exists today").
- Create: `tests/live/test_resume_live.py`
- Mark the design doc `Status: approved 2026-10-26`.

- [ ] **Step 1: Write the live test.** It is opt-in (`ATEAM_LIVE_TESTS=1`) and uses haiku-tier sessions. A scratch target has one contract whose check fails until `src/hint.py` holds `HINT = 1`, and whose acceptance names that test; the contract text does not say how. The test:
  1. runs `ateam run` until it pauses with `budget_exhausted` (`budget.attempts: 1`);
  2. asserts the pause investigation stored findings, or a note;
  3. runs `ateam resume P-0001 --note "set HINT = 1 in src/hint.py"`;
  4. asserts `merge_proposed`.

  Its tier mapping makes `careful` resolve to haiku for cost.
- [ ] **Step 2: Update the docs** per the design's "Spec and CLAUDE.md changes on approval".
- [ ] **Step 3: Run** `.venv/bin/pytest -q`, then `ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live/test_resume_live.py -s`. Read the output; the owner opts in to the live run.
- [ ] **Step 4: Commit** `docs: pause, investigate and resume; live check`
