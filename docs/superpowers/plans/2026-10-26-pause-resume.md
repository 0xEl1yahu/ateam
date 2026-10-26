# Pause, investigate and resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A paused plan has a way out:
- `ateam stop` pauses a run;
- an automatic read-only INVESTIGATE session explains an ATeam pause and suggests an instruction;
- `ateam status P-xxxx` shows it;
- `ateam resume` continues, with refreshed attempts for the paused contracts and the owner's guidance.

Two things come first. Scheduler worktrees must be readable by their own workers, and planning and running must not run at the same time.

**Architecture:**
- **Store.** It records pauses (plan, reason, paused contracts, evidence, findings), attempt windows and stop requests. Contract statuses never change on a pause.
- **Scheduler.** It writes the pause record and counts BUILD attempts only inside the current window. It polls the store for a stop request and, on one, interrupts only the child processes it launched. It launches one pause investigation per paused contract, as a child `ateam worker pause-investigate`, injected in tests.
- **Guidance.** Instructions are YAML files in the intent repo (`guidance/C-xxxx.yaml`). BUILD briefs carry the live ones, meaning those whose contract hash matches.
- **CLI.** Three commands: `ateam stop`, `ateam resume` and `ateam status [PLAN_ID]`.

**Tech Stack:** Python 3.14, click, SQLite (`StateDB`), PyYAML/pydantic, git CLI, `subprocess`, `threading`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-pause-resume-design.md` (approved by the owner 2026-10-26, with decisions 11-13 added at plan review); binding background `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`.

## Global Constraints

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
  - windows open only for the contracts a pause record lists (owner, 2026-10-26): none after `owner_stopped`, and none for a plan paused without a record;
  - an investigation reserves `tiers["careful"][Role.investigate].usd` against `run_budget_usd`.
- **Stop through the store, never a process-group signal.**
  - `ateam stop` records `stop-request:<plan>`. The running scheduler polls it at least every 0.5 s.
  - On a stop request, the scheduler sets its stop event and sends SIGINT to each child it launched, by pid, from its own registry.
  - Verify, refresh and integrate run in their own sessions and are never signalled; they finish.
  - In-process git steps finish before the run returns.
- **Exclusion (owner, 2026-10-26).** While a run holds the lock, these commands refuse with exit 4: `init`, `plan`, `approve`, `reject`, `records review|approve|reject|supersede|retire`, `contracts freeze`, `merge`, `intent pull`, `resume`, and a direct `stop`. While a planning session is live, `run`, `resume` and a direct `stop` refuse. Commands the scheduler runs as children (verify, refresh, integrate, worker run) are not guarded.
- **Policy (owner, 2026-10-26).** A path that resolves inside the worker's own worktree is not "ATeam's home". Nothing else under the home becomes readable: not other worktrees, not the integration worktree, not the intent repo, the state store or other scratch directories.
- **Exit codes:** `stop` 0 (requested or paused) or 4 (refused). `resume` 4 when refused and 1 when its intent commit fails; otherwise `ateam run`'s codes (0, 1, 4, 130), or 0 with `--no-run`.
- **Unchanged rules:**
  - git-native only and no `rm`;
  - nothing pushed or fetched;
  - approval gates unchanged;
  - a paused plan is still refused by `ateam run`, which now names `ateam resume`;
  - Ctrl-C in the run's terminal keeps its meaning (`interrupted`, plan stays `executing`) unless a stop request is recorded.
- **Tests:**
  - real git on scratch targets;
  - no unit test may start a real session: `run_plan` resolves `launch` and `investigate` at call time (`None` means the module default), and an autouse fixture in `tests/conftest.py` makes `ateam.scheduler.default_investigate` raise unless a test replaces it;
  - in `cli_target`, C-0001 is the seed fixture;
  - never `monkeypatch.undo()`.
- **Commits** end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **A resume interrupted between its store windows and its intent commit.** Running `resume --note` again succeeds and leaves exactly one guidance entry (Task 7).
2. **A plan paused before this change, with no pause record.** Resume works, refreshes nothing and says so (Task 7).
3. **`ateam stop` while the scheduler is inside an exclusive step (verify or merge).** The step finishes, nothing is marked `tamper`, and the plan ends `paused (owner_stopped)` (Task 4).
4. **The owner commits in their checkout while a pause investigation runs.** The findings are discarded as tamper, `status` says why, and the pause still resumes (Tasks 5, 6 and 8).
5. **A contract re-frozen between pause and resume.** Old instructions are not delivered, and `--adopt` of a pre-freeze suggestion is refused (Tasks 3 and 7).

---

### Task 0a: A worker may read its own worktree under ATeam's home

**Files:**
- Modify: `src/ateam/runner/policy.py` (`_under_home_not_scratch` and the other home checks that call it: lines ~1068, 1098-1111, 1129, 1228, 1308-1317); `CLAUDE.md` (Conventions: one line, given below).
- Test: `tests/test_policy.py`

**Interfaces:**
- Produces: `Policy._in_own_worktree(path: Path) -> bool`. It is true when the resolved path equals `self.worktree.resolve()` or lies under it. Every home check treats such a path as not-home.

- [ ] **Step 1: Write the failing table rows** (a fixture with `ateam_home = tmp/.ateam`, `worktree = tmp/.ateam/<id>/worktrees/C-0001`, `scratch = tmp/.ateam/<id>/scratch/C-0001`, for BUILD, TEST and INVESTIGATE):
  - **Allowed:**
    - `Read <wt>/src/x.py`;
    - `Grep` in `<wt>`;
    - `cat src/x.py`;
    - `cat <wt>/src/x.py`;
    - `ls <wt>`;
    - `git -C <wt> log` (BUILD/TEST, where git is allowed).
  - **Denied:**
    - `cat <home>/<id>/worktrees/C-0002/src/x.py` (another contract's worktree);
    - `cat <home>/<id>/worktrees/P-0001/src/x.py` (the integration worktree);
    - `cat <home>/<id>/intent/config.yaml`;
    - `cat <home>/<id>/state.db`;
    - `cat <home>/<id>/scratch/C-0001-test/x`;
    - `cat <wt>/../C-0002/src/x.py`;
    - a symlink inside `<wt>` that points at `<home>/<id>/intent`, read through `Read` and `cat`;
    - `cat <wt>-evil/x` (a sibling whose name has the worktree's name as a prefix).
- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_policy.py -k own_worktree`. Expected: the allowed rows fail with a home denial.
- [ ] **Step 3: Implement it.** Resolve the path first, then apply the own-worktree test, so a link out of the worktree is still judged by its target. Add to CLAUDE.md under Conventions: `- Scheduler worktrees live under ATeam's home (<home>/worktrees/<cid>); policy tests for home reads must include a worktree under the home, not only one outside it.`
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_policy.py tests/test_run.py tests/test_investigate.py`. Expected: all pass, and the xfail count is unchanged.
- [ ] **Step 5: Commit** `fix(policy): a worker reads its own worktree under ATeam's home (owner, 2026-10-26)`

---

### Task 0b: Planning and running exclude each other

**Files:**
- Create: `src/ateam/exclusion.py`
- Modify: `src/ateam/cli.py` (guard calls, plus the existing `_process_alive` moved into `exclusion.py` and imported back); `src/ateam/scheduler.py` (`run_plan` refuses while planning is live).
- Test: `tests/test_exclusion.py` (new), `tests/test_cli.py`, `tests/test_scheduler.py`

**Interfaces:**
- Produces:
  - in `ateam.exclusion`:
    - `process_alive(pid: int) -> bool`;
    - `live_planning(db: StateDB) -> list[tuple[str, int]]`: `(plan_id, pid)` for each `investigating` feature whose `planning:<id>` marker names a live process;
    - `run_holder(db: StateDB) -> dict | None`: `run_lock_info()` plus `"alive": bool`;
  - in `ateam.cli`:
    - `require_no_live_run(ctx) -> None` refuses with exit 4: `"{plan} is running (pid {pid}); wait for it or stop it: ateam stop {plan}"`, or for a dead pid `"the run lock names {plan} (pid {pid}), a process that is gone; remove the lock by hand"`;
    - `require_no_live_planning(ctx) -> None` refuses with exit 4: `"{plan} is being planned (pid {pid}); wait for it to finish"`.
  - `run_plan` raises `RunRefused` with the same planning message, after taking the lock and before preflight.
- The guarded commands are those in Global Constraints. Each calls `require_no_live_run(ctx)` before its first write.

- [ ] **Step 1: Write the failing tests:**
  - `tests/test_exclusion.py`: `live_planning` ignores a dead marker and a non-investigating plan; `run_holder` reports `alive`.
  - `tests/test_cli.py`: a parametrized `test_intent_commands_refuse_while_a_run_holds_the_lock`, one row per guarded command with minimal valid arguments. It inserts a lock row with `os.getpid()` and expects exit 4 with "is running", and no new intent commit. Add `test_stale_run_lock_refusal_names_the_dead_process`.
  - `tests/test_scheduler.py`: `test_run_refuses_while_a_plan_is_being_planned`, with a marker for this test's pid on an `investigating` feature. The lock is released afterwards.
- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_exclusion.py tests/test_cli.py tests/test_scheduler.py -k "exclusion or holds_the_lock or stale_run_lock or being_planned"`
- [ ] **Step 3: Implement it.** Update the README's planning paragraph: the "Run one `ateam plan` per target at a time" sentence gains "and never during `ateam run`; each refuses while the other is live". Update the spec bullet "One running plan per target" to: "a second plan can be investigated and approved only while no run is live; planning and running exclude each other (owner, 2026-10-26)."
- [ ] **Step 4: Run** `.venv/bin/pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: planning and running exclude each other (owner, 2026-10-26)`

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
        for t in ("pauses", "pause_contracts", "attempt_windows"):
            db.conn.execute(f"DROP TABLE {t}")
    db.init_schema()
    assert db.open_pause("P-0001") is None and db.window_start("C-0001") == 0
```

- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_state_db.py -k "pause or window or stop_request"`. Expected: `AttributeError: 'StateDB' object has no attribute 'record_pause'`.
- [ ] **Step 3: Implement it.** JSON columns use `json.dumps(..., default=str)` and `json.loads` on read; `conflict` is stored as 0/1 and returned as bool.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_state_db.py`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(state): pause records, attempt windows and stop requests`

---

### Task 2: Pause reasons tell the truth (#17), plus `owner_stopped`

**Files:**
- Modify: `src/ateam/retry.py`; `src/ateam/cli.py` (`verify` and `worker_run` result files); `src/ateam/scheduler.py` (pass `refusal` into `StepOutcome`).
- Test: `tests/test_retry.py`, `tests/test_cli.py`, `tests/test_scheduler.py`

**Interfaces:**
- Produces:
  - `PAUSE_REASONS` includes `"owner_stopped"`.
  - `StepOutcome.refusal: str | None = None`.
  - The verify and worker run result files gain `"refusal"`:
    - `"records_drifted"` for a `ContractNotBinding` while `drift(contract, records)` is non-empty;
    - `"not_binding"` for any other `ContractNotBinding`;
    - `"intent_dirty"` for an `IntentError`;
    - `"other"` for any other refusal;
    - `null` when the command did not refuse.
  - `classify` returns `"drifted"` when `refusal == "records_drifted"`. It returns `"not_binding"` for a verify refused with `"not_binding"`, and for a worker outcome `not_binding` (code 4) without drift. It returns `"refused"` otherwise.
  - The `_PAUSE_NOW` map:

    | class | pause reason |
    |---|---|
    | `requirements` | `decision_change_needed` |
    | `drifted` | `records_drifted` |
    | `not_binding` | `verification_unresolvable` |
    | `refused` | `environment_failure` |
    | `tamper` | `verification_unresolvable` |

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("stage,code,outcome,status,refusal,reason", [
    ("verify", 4, "refused", "refused", "records_drifted", "records_drifted"),
    ("verify", 4, "refused", "refused", "not_binding", "verification_unresolvable"),
    ("verify", 4, "refused", "refused", "intent_dirty", "environment_failure"),
    ("verify", 1, "refused", "refused", "other", "environment_failure"),
    ("build", 4, "not_binding", None, "records_drifted", "records_drifted"),
    ("build", 4, "not_binding", None, "not_binding", "verification_unresolvable"),
    ("build", 1, "refused", None, "other", "environment_failure"),
])
def test_refusals_pause_with_their_cause(stage, code, outcome, status, refusal, reason):
    o = StepOutcome(stage, code, outcome, verify_status=status, refusal=refusal)
    assert decide(classify(o), 0, 2, "light", False, None, stage=stage, stage_used=1).reason == reason


def test_owner_stopped_is_a_pause_reason():
    assert "owner_stopped" in PAUSE_REASONS
```

  In `tests/test_cli.py`: `test_verify_result_file_names_the_refusal`. It writes a stray file in the intent repo and expects exit 4 with `refusal == "intent_dirty"`; then it hand-edits the contract after freeze and expects `refusal == "not_binding"`.

- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_retry.py tests/test_cli.py -k "refusal or owner_stopped"`
- [ ] **Step 3: Implement it.**
  - Set `refusal` in the existing `except` branches of `verify` and `worker_run`, using `drift(contract, load_records(...))` for the drift test.
  - The `_Result` exit path sets `"other"` when a refusal left it `None` with status or outcome `refused`.
  - Update every existing scheduler test that asserted the old reason, and say which in the commit message.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_retry.py tests/test_cli.py tests/test_scheduler.py`. Expected: all pass.
- [ ] **Step 5: Commit** `fix(retry): refusals pause with their cause; owner_stopped reason (#17)`

---

### Task 3: Guidance: storage and delivery to BUILD

**Files:**
- Create: `src/ateam/guidance.py`
- Modify: `src/ateam/paths.py` (property), `src/ateam/brief.py` (section), `src/ateam/runner/run.py` (load for BUILD), and the `brief` command in `src/ateam/cli.py`, which passes the same instructions.
- Test: `tests/test_guidance.py` (new), `tests/test_brief.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `TargetPaths.guidance` property returning `self.intent / "guidance"`.
  - In `ateam.guidance`:
    - `class GuidanceEntry(BaseModel)` with `at: datetime`, `source: str` (`"owner"` or `"A-<attempt_id>"`), `contract_hash: str` and `text: str`;
    - `load_guidance(directory: Path, contract_id: str) -> list[GuidanceEntry]`, which returns `[]` when the file is missing;
    - `append_guidance(directory: Path, contract_id: str, entry: GuidanceEntry) -> Path`, which writes `<directory>/<contract_id>.yaml` as `{contract: <id>, entries: [...]}`;
    - `live_guidance(entries: list[GuidanceEntry], contract_hash: str) -> list[GuidanceEntry]`.
  - `Brief.instructions: str | None = None`.
  - `assemble_brief(..., instructions: list[GuidanceEntry] = ())` fills it only for `Role.build`.
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

  In `tests/test_cli.py`: `test_brief_command_carries_only_live_guidance`. On a frozen C-0001, append one entry with the current `content_hash()` and one with `"stale"`. `ateam brief C-0001 --role build` shows the first and not the second; `--role test` shows neither.

- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_guidance.py tests/test_brief.py tests/test_cli.py -k "guidance or instructions"`. Expected: an ImportError for `ateam.guidance`.
- [ ] **Step 3: Implement it.** `run_contract_attempt` and `brief` pass `live_guidance(load_guidance(paths.guidance, contract.id), contract.content_hash())` for `Role.build` and `()` otherwise.
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_guidance.py tests/test_brief.py tests/test_run.py tests/test_cli.py`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(guidance): owner instructions in the intent repo reach BUILD briefs`

---

### Task 4: Scheduler: pause records, attempt windows, owner stop

**Files:**
- Modify: `src/ateam/scheduler.py`, `src/ateam/integrate.py` (`_pause` records a pause).
- Test: `tests/test_scheduler.py`, `tests/test_integrate.py`

**Interfaces:**
- Consumes: Task 1's `record_pause`, `window_start`, `stop_requested` and `clear_stop`.
- Produces:
  - **`_Outcome`** gains `evidence: dict | None = None` (the result dict of the step that paused: the last `r` or `v`) and `conflict: bool = False` (True for the `MergeConflict` pause in `_merge`).
  - **On pause,** after setting the plan paused, `_Run.execute` calls `db.record_pause(plan_id, first.reason, first.detail, [one dict per paused outcome], conflict=any(o.conflict for o in pauses))`, with `contract_hash = db.contract_hash(cid)`.
  - **`_counted_builds`** keeps only BUILD attempts with `attempt_id > db.window_start(cid)` and an outcome other than `interrupted`.
  - **Child registry.** A module-level `_Children`:
    - `add(proc: subprocess.Popen)`, `discard(proc)` and `interrupt_all() -> int`, the last sending SIGINT by pid;
    - `default_launch` uses `Popen` in this process group and registers the child while it runs;
    - `run_child` (verify, refresh, integrate) does not register;
    - `run_plan(..., launch: Launch | None = None, investigate: Investigate | None = None)` resolves `None` to the module default at call time.
  - **Stop polling.** In the main loop, at most every 0.5 s, if `db.stop_requested(plan_id)` it sets `self.stop` and calls `_Children.interrupt_all()`.
  - **Owner stop.** When the loop ends with `self.stop` set and `db.stop_requested(plan_id)`, it:
    1. closes open attempts;
    2. sets the plan paused, why `"owner_stopped: stopped by the owner"`;
    3. calls `record_pause(plan_id, "owner_stopped", "stopped by the owner", [])` and `clear_stop(plan_id)`;
    4. returns outcome `"paused"`.

    A stop request present when the run starts is cleared after the lock is taken.
  - **Preflight refusal** for a paused plan: `"{plan_id} is paused ({reason}); resume it with: ateam resume {plan_id}"`.
  - **`integrate._pause(paths, db, plan, outcome, why, ...)`** records the pause:
    - reason and detail come from splitting `why` at the first `": "`;
    - `conflict=(outcome == "conflict")`;
    - for the contract conflict at integrate.py:142 the contracts list holds that cid, with `evidence={"paths": e.paths}`; the target conflict holds none.

- [ ] **Step 1: Write the failing tests** (the `plan3` and `FakeWorkers` helpers already in the file)

```python
def test_a_pause_is_recorded_with_its_contract_and_evidence(plan3):
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}})
    s, out = _run(plan3, fake)
    db = _db(plan3); p = db.open_pause("P-0001")
    assert s.outcome == "paused" and p["reason"] == "decision_change_needed" and not p["conflict"]
    [c] = p["contracts"]
    assert c["contract_id"] == "C-0001" and c["evidence"]["test_class"] == "requirements"
    assert c["contract_hash"] == db.contract_hash("C-0001") and db.contract_status("C-0001") == "ready"


def test_a_window_refreshes_attempts_and_escalation(plan3):
    turns = {"code": 1, "outcome": "failed", "subtype": "error_max_turns", "commit": False}
    _run(plan3, FakeWorkers({("C-0001", "build"): turns}))          # escalates once, then budget_exhausted
    db = _db(plan3); pause = db.open_pause("P-0001")
    used = len(db.attempts_for("C-0001", role="build"))
    db.resume_pause(pause["pause_id"], ["C-0001"])
    set_plan_status(plan3.paths, db, load_plans(plan3.paths.plans)["P-0001"], ["executing"], by="t")
    fake2 = FakeWorkers()
    s, out = _run(plan3, fake2)
    assert s.outcome == "merge_proposed" and fake2.tiers("C-0001")[0] is None      # the frozen tier again
    assert len(_db(plan3).attempts_for("C-0001", role="build")) == used + 1


def test_owner_stop_pauses_owner_stopped(plan3):
    def owner_stop(fake, cid, role, worktree, n):
        _db(plan3).request_stop("P-0001")
        time.sleep(0.8)                                               # the loop polls within 0.5 s
        return {"code": 130, "outcome": "interrupted", "commit": False}

    s, out = _run(plan3, FakeWorkers({("C-0001", "build", 0): owner_stop}))
    db = _db(plan3)
    assert s.outcome == "paused" and _last_reason(plan3) == "owner_stopped: stopped by the owner"
    assert db.open_pause("P-0001")["contracts"] == [] and not db.stop_requested("P-0001")
    assert db.open_attempts(list(NAMES)) == []
```

  More tests:
  - `test_merge_conflict_pause_is_marked_conflict`, built on `conflicting_merge_lands_first` from the existing conflict test: `open_pause("P-0001")["conflict"] is True`.
  - `test_owner_stop_during_an_exclusive_step_finishes_it_without_tamper` (Review Focus 3). Wrap `child` so the verify child calls `request_stop` before it runs. Assert the verify recorded its verdict in the store, no attempt has outcome `tamper`, the plan is paused `owner_stopped`, and the integration worktree has no `index.lock`.
  - `test_default_launch_registers_and_interrupt_all_signals_only_children`. Launch `sleep 30` through a `Popen` stand-in, check `interrupt_all() == 1`, and confirm the process ended with SIGINT. This process is not signalled.
  - `test_paused_plan_refusal_names_resume`.
  - In `tests/test_integrate.py`: `test_integration_conflict_records_a_conflict_pause`, using the file's existing conflict fixture: `conflict` is True, and the contracts are `[cid]` for a contract conflict.

- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_scheduler.py tests/test_integrate.py -k "pause or window or owner_stop or interrupt_all or names_resume"`
- [ ] **Step 3: Implement it.** In CLAUDE.md under Conventions add: `- Never signal a process group ATeam did not create; the scheduler interrupts its own children by pid.`
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_scheduler.py tests/test_integrate.py tests/test_retry.py tests/test_run_command.py` three times. Expected: all pass each time.
- [ ] **Step 5: Commit** `feat(scheduler): pause records, attempt windows and owner stop through the store`

---

### Task 5: The pause investigation session

**Files:**
- Modify:
  - `src/ateam/roles.py`: `PAUSE_SCHEMA` and `PAUSE_INVESTIGATE_SPEC`.
  - `src/ateam/brief.py`: `assemble_pause_brief`.
  - `src/ateam/runner/investigate.py`: cwd and policy from `target_git.root`, plus `scratch_name`.
  - `src/ateam/cli.py`: `worker pause-investigate`.
- Test: `tests/test_roles.py`, `tests/test_brief.py`, `tests/test_investigate.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 0a (the policy allows the worktree); Task 1's `open_pause` and `set_pause_findings`; Task 3's `load_guidance` and `live_guidance`.
- Produces:
  - `PAUSE_SCHEMA`: an object requiring `summary` (string), `evidence` (an array of `{ref: string, shows: string}`), `failure_class` (enum `implementation`, `requirements`, `environment`, `invariant_violation`), `needs_owner_decision` (string or null) and `suggested_instruction` (string or null).
  - `PAUSE_INVESTIGATE_SPEC = RoleSpec(role=Role.investigate, tools=_INVESTIGATE_TOOLS, ..., report_schema=PAUSE_SCHEMA)`. Its system prompt states the role is read-only and diagnostic, and that `suggested_instruction` must be guidance within the frozen contract and never a change to it.
  - `assemble_pause_brief(contract, records: list[Record], paused: dict, attempts: list[dict], build_checkpoint: Checkpoint | None, instructions: list[GuidanceEntry]) -> str`. Sections, in order:
    - `## Binding`;
    - `## Task`;
    - `## Why the plan paused`: the reason and detail, plus the evidence JSON pretty-printed, capped at 12 000 characters;
    - `## Attempt history`: one line per attempt with role, tier, outcome, failure_class and cost;
    - `## Previous worker's notes (unverified)`, from the BUILD checkpoint;
    - `## Instructions already given`.
  - `run_investigation(..., scratch_name: str | None = None)`, where cwd and `Policy.worktree` become `target_git.root`. Existing callers pass the owner's checkout, so their behaviour is unchanged.
  - CLI `ateam worker pause-investigate PLAN_ID CONTRACT_ID --cwd PATH [--tier careful] --result-file PATH --target PATH`. It:
    - validates `--cwd` with `_require_isolated_worktree`;
    - refuses with exit 4, `"outcome": "refused"`, when there is no open pause naming the contract;
    - runs `run_investigation(name=cid, scratch_name=f"{cid}-investigate", target_git=GitRepo(cwd), spec=PAUSE_INVESTIGATE_SPEC, ...)`;
    - always writes the findings with `set_pause_findings(pause_id, cid, attempt_id, report or None, note)`, where `note` is `None` when completed, `"tamper: <first change>"` when tampered and `"no report"` otherwise;
    - result-file fields: `code`, `outcome` (`completed`, `tamper`, `failed`, `refused` or `interrupted`), `attempt_id`, `cost_usd` and `error`;
    - exit codes: 0 completed, 1 tamper or failed, 4 refused.

- [ ] **Step 1: Write the failing tests:**
  - `test_pause_schema_requires_the_five_fields` (roles).
  - `test_pause_brief_sections_in_order` (brief): it asserts each heading appears in order, the evidence is capped at 12 000 characters, and only live guidance is listed.
  - `test_investigation_runs_in_the_given_checkout` (investigate): a fake `run_worker` captures `options.cwd == target_git.root`, and `policy.worktree` equals it too.
  - `test_investigation_in_a_worktree_sees_a_shared_ref_change_as_tamper` (investigate; Review Focus 4): the fake worker creates a branch in the owner's checkout during the session, then asserts "refs changed" is in `tampered` and `res.report is None`.
  - `test_pause_investigate_records_findings_and_refuses_without_a_pause` (cli): with `run_investigation` monkeypatched to return a canned report, the findings are stored and the result file says `completed`; with a tampered result, the findings are `None` and the note starts with `"tamper:"`; with no open pause, exit 4 and outcome `refused`.
- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_roles.py tests/test_brief.py tests/test_investigate.py tests/test_cli.py -k "pause or given_checkout or shared_ref"`
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_roles.py tests/test_brief.py tests/test_investigate.py tests/test_cli.py tests/test_policy.py`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(investigate): pause investigation session in the contract worktree`

---

### Task 6: Scheduler launches pause investigations

**Files:**
- Modify: `src/ateam/scheduler.py`; `tests/conftest.py` (the autouse guard); `tests/test_scheduler.py` (the `_run` helper passes `investigate=FakeInvestigator()` unless given); `tests/test_run_command.py` (`_fake` patches `default_launch` and `default_investigate`).
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Task 4's pause record and `_Children`; Task 5's command and result-file fields.
- Produces:
  - `Investigate = Callable[[Path, str, str, Path, Path], dict]`, called as `(target, plan_id, cid, worktree, result_file)`.
  - `default_investigate(...)` runs `ateam worker pause-investigate`, registered in `_Children` like `default_launch`.
  - After `record_pause`, and only when the reason is not `owner_stopped`, there is one investigation per paused contract, sequentially, in contract-id order:
    1. With no worktree at `<home>/worktrees/<cid>`, it records the note `"no worktree to investigate"`.
    2. Otherwise it calls `self.budget.reserve(usd, self.stop)`. This is the stop event, not `self.halt`, which every pause has already set. On `"exhausted"` it records the note `"run budget ${limit:.2f} would be exceeded by the investigation"`, and on `"halted"` the note `"interrupted"`.
    3. Otherwise it calls `investigate`, settles the actual cost, and says `"{cid} investigate {outcome} ${cost:.2f}"`.
    4. Afterwards, if the record still has neither findings nor a note for the cid, it records the note `f"investigation {outcome}: {error or 'no findings written'}"`.
    5. When `self.stop` is set, the remaining investigations get the note `"interrupted"`.
  - The plan is paused before the first investigation starts.

- [ ] **Step 1: Write the failing tests**

```python
class FakeInvestigator:
    def __init__(self, report=None, outcome="completed", write=True):
        self.calls, self.outcome, self.write = [], outcome, write
        self.report = report or {"summary": "s", "evidence": [], "failure_class": "implementation",
                                 "needs_owner_decision": None, "suggested_instruction": "use x"}

    def __call__(self, target, plan_id, cid, worktree, result_file):
        db = StateDB(home_dir(target) / "state.db"); db.init_schema()
        self.calls.append((cid, db.feature_status(plan_id), worktree))
        if self.write:
            pause = db.open_pause(plan_id)
            findings = self.report if self.outcome == "completed" else None
            db.set_pause_findings(pause["pause_id"], cid, None, findings, None if findings else f"{self.outcome}: x")
        r = {"code": 0 if self.outcome == "completed" else 1, "outcome": self.outcome, "attempt_id": None,
             "cost_usd": 0.2, "error": None}
        result_file.write_text(json.dumps(r)); return r


def test_an_ateam_pause_investigates_each_paused_contract_once(plan3):
    inv = FakeInvestigator()
    fake = FakeWorkers({("C-0001", "test"): {"code": 5, "test_outcome": "failed", "test_class": "requirements"}})
    s, out = _run(plan3, fake, investigate=inv)
    assert [c[0] for c in inv.calls] == ["C-0001"] and inv.calls[0][1] == "paused"
    assert inv.calls[0][2] == home_dir(plan3.root) / "worktrees" / "C-0001"
    assert _db(plan3).open_pause("P-0001")["contracts"][0]["findings"]["suggested_instruction"] == "use x"
```

  More tests:
  - `test_owner_stop_is_not_investigated`: as `test_owner_stop_pauses_owner_stopped`, with `inv.calls == []`.
  - `test_a_tampered_investigation_leaves_the_pause_standing`: `FakeInvestigator(outcome="tamper")` leaves the findings `None`, a note starting `"tamper"`, and the plan paused.
  - `test_a_crashed_investigation_gets_a_note`: `FakeInvestigator(outcome="failed", write=False)` gets the note `"investigation failed: no findings written"`.
  - `test_investigation_that_does_not_fit_the_budget_leaves_a_note`, with `max_workers=1` and `run_budget_usd` sized to the BUILD, TEST and verify path that pauses: the note starts `"run budget"` and `inv.calls == []`.
  - `test_unit_tests_cannot_start_a_real_investigation`: calling `ateam.scheduler.default_investigate` under the conftest guard raises.
- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_scheduler.py -k "investigat"`
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_scheduler.py tests/test_run_command.py` three times. Expected: all pass each time.
- [ ] **Step 5: Commit** `feat(scheduler): investigate each paused contract automatically`

---

### Task 7: `ateam resume` (and `ateam run`'s foreground loop shared)

**Files:**
- Modify: `src/ateam/cli.py`; `src/ateam/scheduler.py`.
  - Extract `_run_foreground(target: Path, plan_id: str, dry_run: bool) -> NoReturn` from `run_cmd`. It passes `launch=scheduler.default_launch, investigate=scheduler.default_investigate` explicitly, and `run_cmd` calls it.
  - Extract the per-contract checks of `scheduler._preflight` into `scheduler.binding_problems(ctx, cids: list[str]) -> list[str]`; `_preflight` raises on the first problem.
- Test: `tests/test_run_command.py`

**Interfaces:**
- Consumes: Task 0b (`require_no_live_run`, `require_no_live_planning`); Task 1 (`open_pause`, `resume_pause`); Task 3 (`append_guidance`, `GuidanceEntry`, `TargetPaths.guidance`); `set_plan_status(..., extra_files=...)`; `require_clean`.
- Produces: `ateam resume PLAN_ID [--adopt | --note TEXT | --note-file PATH] [--contract C-xxxx]... [--no-run] [--target]`. In order:
  1. **Refusals (exit 4).**
     - The plan is not `paused` in the store or its file.
     - `require_no_live_run`.
     - `require_no_live_planning`.
     - `require_clean` on the intent repo.
     - A conflict pause: `"{plan} paused on a merge conflict ({detail}); resume is refused until the rebase BUILD exists"`.
     - `binding_problems(ctx, plan_contract_ids(db, plan))` reports anything, the same set `_preflight` checks; every problem is named.
  2. **Which contracts the instruction is for.**
     - With `--contract`, those named, each of which must belong to the plan. Otherwise the paused contracts of the open record, if there is exactly one.
     - Refused: `--note` with zero or several candidates and no `--contract`; `--adopt` with zero candidates; `--adopt` together with `--note` or `--note-file`; any instruction when every unfinished contract is `integrated`.
     - `--adopt` takes each candidate's `findings.suggested_instruction`. It refuses when any is missing, and when the record's `contract_hash` differs from `db.contract_hash(cid)`: `"the suggestion for {cid} was written before its re-freeze"`.
  3. **Windows.** They open for the paused contracts of the open record (`resume_pause(None, cids)`). With no record, or a record listing no contracts, no windows open, and the command prints `"no contract's attempts were refreshed"`.
  4. **Instructions.** Each is appended as `GuidanceEntry(source="owner" or f"A-{attempt_id}", contract_hash=contract.content_hash(), ...)`. Then `set_plan_status(paths, db, plan, ["executing"], by="owner", why="resumed", extra_files=[guidance files])` makes one intent commit. If that commit fails, it prints `git -C "<intent>" checkout -- <each written file>` and exits 1, leaving the plan paused and nothing else changed.
  5. **The record is closed:** `db.resume_pause(pause_id, [])` when there is one.
  6. **The run:** unless `--no-run`, `_run_foreground(target, plan_id, False)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_run_command.py`, reusing `plan3` and `FakeWorkers` from `tests/test_scheduler.py`, with `_fake` patching both launchers):
  - `test_resume_refusals`, table-driven, one row each:
    - not paused;
    - run lock held;
    - planning live;
    - dirty intent repo;
    - conflict pause;
    - an unfinished contract hand-edited after freeze;
    - `--adopt` with no suggestion;
    - `--adopt` after `owner_stopped` (zero candidates);
    - `--adopt` of a suggestion recorded under an older contract hash;
    - `--adopt --note`;
    - `--note` with two paused contracts and no `--contract`;
    - `--note` when all are integrated.

    Each row expects exit 4 and a message naming the cause; afterwards `feature_status` is still `paused` and the intent HEAD is unchanged.
  - `test_resume_with_a_note_commits_guidance_and_runs_to_merge_proposed`: after a `budget_exhausted` pause, `ateam resume P-0001 --note "use x"` exits 0 and the run ends `merge_proposed`. Exactly one new intent commit touches `plans/` and `guidance/C-0001.yaml`. `ateam brief C-0001 --role build` contains `"owner: use x"`.
  - `test_resume_adopt_records_the_investigation_source`: the entry's source is `A-<attempt_id>` from the record.
  - `test_resume_no_run_only_changes_state`.
  - `test_resume_after_a_failed_intent_commit_resumes_again` (Review Focus 1): monkeypatch `commit_intent_files` to raise once. The first `resume --note "use x"` exits 1 and prints the checkout restore command. Run that command, run `resume --note "use x"` again: exit 0 and exactly one guidance entry.
  - `test_resume_of_a_plan_paused_without_a_record` (Review Focus 2): pause through `set_plan_status(["paused"])` with no record. `resume --no-run` exits 0, prints "no contract's attempts were refreshed", and `window_start` is 0 for every contract.
  - `test_refrozen_contract_gets_a_window_and_no_stale_guidance` (Review Focus 5): add guidance, change and re-freeze C-0001 with `--force`, then `resume --no-run` with no instruction. `window_start("C-0001") > 0`, and `ateam brief C-0001 --role build` has no "Owner's instructions".
- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_run_command.py -k resume`
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run** `.venv/bin/pytest -q tests/test_run_command.py tests/test_scheduler.py tests/test_cli.py`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(cli): ateam resume with owner guidance and refreshed attempts`

---

### Task 8: `ateam stop` and `ateam status [PLAN_ID]`

**Files:**
- Modify: `src/ateam/cli.py` (`stop`, `status`).
- Test: `tests/test_run_command.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 0b (`run_holder`, the guards); Task 1 (`request_stop`, `stop_requested`, `record_pause`, `open_pause`); `set_plan_status`.
- Produces:
  - `ateam stop PLAN_ID [--target]`.
    - **Refusals (exit 4):** the plan is not `executing`; the lock is held by another plan; the lock names this plan with a dead pid ("remove the lock by hand first"); a stop request is already set while the holder is alive ("a stop is already in progress").
    - **Live run of this plan:** `request_stop(plan)` and print `"stop requested; {plan} pauses after its running sessions are interrupted"`.
    - **No lock:** `require_no_live_planning`, take the run lock for the duration, set the plan paused with why `"owner_stopped: stopped by the owner"`, record the pause with no contracts, release the lock, and print `"{plan} -> paused (owner_stopped)"`. Exit 0.
  - `ateam status [PLAN_ID]`.
    - **Without an argument:** the output is unchanged, plus `"  {pid} paused ({reason}): ateam status {pid}"` for each paused plan.
    - **A paused plan:** the reason and detail; per paused contract, its reason, worktree path, findings summary, failure class, evidence lines, `needs_owner_decision` and suggested instruction, or the `note` when there are no findings. Then the three resume commands, exactly as in the spec's "What the owner sees" with the plan id filled in. A conflict pause prints `"resume is refused until the rebase BUILD exists"` instead of the commands.
    - **A plan that is not paused:** its status line and its contracts.

- [ ] **Step 1: Write the failing tests:**
  - `test_stop_from_another_process_pauses_a_running_scheduler` (required by the design). Run `run_plan` in a thread with a fake BUILD that blocks until `stop` is set (polling `threading.Event` with a 10 s cap). From the main thread, call `ateam stop P-0001` through CliRunner; it exits 0. The run ends `paused`, the reason is `owner_stopped: stopped by the owner`, the BUILD attempt is `interrupted`, and the lock is released.
  - `test_stop_without_a_run_pauses_directly`: one intent commit, and the pause record has no contracts.
  - `test_stop_refusals`: not executing; another plan's lock; a stale lock for this plan; stop already in progress.
  - `test_status_shows_findings_and_resume_commands`: a seeded record with findings, and one with a `tamper:` note (Review Focus 4).
  - `test_status_for_a_conflict_pause_refuses_resume`.
- [ ] **Step 2: Run them and watch them fail.** Run `.venv/bin/pytest -q tests/test_run_command.py tests/test_cli.py -k "stop or status"`
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

- [ ] **Step 1: Write the live test.** It is opt-in (`ATEAM_LIVE_TESTS=1`) and every tier maps to haiku. A scratch target has one contract with `budget.attempts: 1`; its check fails until `src/hint.py` holds `HINT = 1`, and the contract text does not say how. The test:
  1. runs `ateam run` until it pauses with `budget_exhausted`;
  2. asserts the pause investigation read the worktree (an attempt with role `investigate`, outcome `completed`) and stored findings;
  3. runs `ateam resume P-0001 --note "set HINT = 1 in src/hint.py"`;
  4. asserts `merge_proposed`.
- [ ] **Step 2: Update the docs** per the design's "Spec and CLAUDE.md changes on approval" and decisions 11-14.
- [ ] **Step 3: Run** `.venv/bin/pytest -q`. The live run waits for the owner to opt in: `ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live/test_resume_live.py -s`. Read its output.
- [ ] **Step 4: Commit** `docs: pause, investigate and resume; live check`
