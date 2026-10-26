# Scheduler (`ateam run`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ateam run P-xxxx` takes an approved plan from `plan_approved` to `merge_proposed` without the owner driving each contract by hand, and stops at the owner's merge gate or at a pause.

**Architecture:** `src/ateam/scheduler.py` holds `run_plan`.
- **Workers:** threads each run one contract's pipeline. The BUILD and TEST sessions run as `ateam worker run` subprocesses. `ateam verify` and `ateam contracts refresh` run as subprocesses in their own process session, so a terminal Ctrl-C never interrupts them. Each child reports through a `--result-file` JSON.
- **Merges and reconciliation** reuse public helpers from `integrate.py`.
- **Gate:** a writer-preferring gate. Sessions hold it shared. Every step that commits to the intent repo or creates refs holds it exclusive: refresh, verify, merge-at-verify, plan status, worktree creation.
- **Decisions** sit in two pure modules: `retry.py` (retry decisions) and `worktrees.py` (contract worktrees).

**Tech Stack:** Python 3.14, click, SQLite (`StateDB`), git CLI, `subprocess`, `threading`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md` ("Architecture: scheduler", "Allocation", "Verification and integration" steps 1–4, "Context lifecycle", pause conditions). Reference only: P-0017 in the intent repo and its review (`intent/plans/P-0017-*.yaml`), never approved.

## Global Constraints

- **Reconciliation basis:** the spec first, the owner's rulings where they override it, and main's code as a constraint changed only through a task below.
- **Unchanged rules:** git-native only; no `rm`; nothing pushed or fetched; a contract runs only when it binds; approval gates unchanged; intent repo first, store second; re-runs reconcile a crash between the two.
- **TEST isolation:** TEST never receives the BUILD report. It gets its own scratch directory, `<home>/scratch/<contract-id>-test`.
- **Concurrency:** at most `config.max_workers` sessions at once. No session runs while an exclusive step runs. The tamper checks are not narrowed.
- **Worktrees:** `<home>/worktrees/<contract-id>` on branch `ateam/<contract-id>`, cut from the integration-branch tip when the contract becomes ready, with that tip recorded as `base:<contract-id>` in meta first. Never removed.
- **Retries:**
  - The limit is the contract's frozen `budget.attempts` BUILD attempts, counted from the store across runs.
  - When exhausted: environment pauses with `environment_failure`, a repeated scope breach with `scope_exceeded`, everything else with `budget_exhausted`.
  - Escalation is one tier up, once per contract, only after a BUILD that ended with subtype `error_max_turns`, and never above `careful`. Environment retries never change the tier.
  - The rubric's frozen tier is used as is; there is no tier cap (owner, 2026-10-26).
- **Run budget:** `config.run_budget_usd` (default 10.0). Before launching a session, if the spend in this run plus that session's tier cap (`usd`) would exceed it, launch nothing more, let running stages finish, and pause with `budget_exhausted`.
- **Pause and conflict:**
  - Pause reasons come only from the spec enum: decision_change_needed, scope_exceeded, budget_exhausted, verification_unresolvable, sensitive_action, environment_failure, records_drifted.
  - A conflict at merge-at-verify pauses with `environment_failure: <cid> conflicts in <paths>`, and the contract stays `verified`.
  - A paused plan is refused by `ateam run`, naming the recorded reason. The rebase BUILD, resume and INVESTIGATE-before-BUILD are deferred and recorded as spec deviations.
- **Exit codes:** `ateam run` exits 0 at merge_proposed, 1 when paused or stalled, 4 when refused or when a re-run is needed, 130 when interrupted. `--dry-run` launches and writes nothing.
- **Tests:** real git on a scratch target. Fake worker sessions are injected through `run_plan(..., launch=...)`, never through env hooks in production code. In `cli_target`, C-0001 is the seed fixture.

## Review Focus

1. **A second `ateam run` while one is running, or after a `kill -9`.** The second run must be refused, naming the holder's pid. It never steals the lock.
2. **Ctrl-C during a BUILD session.** The attempt row must end `interrupted`, not stay open forever. The next run continues from the worktree without redoing verified contracts.
3. **A verify or merge commit while a session runs.** It must never happen. With `max_workers=2`, no attempt is marked `tamper` by the intent HEAD check.
4. **Budget and escalation.** The run must not overshoot `run_budget_usd` by more than one in-flight session. An `error_max_turns` BUILD escalates exactly once.
5. **A dependent contract.** It is cut after its dependency merged, so it contains the dependency's files, and verify (with `--base`) judges only its own diff.

---

### Task 1: State store support

**Files:** modify `src/ateam/state/db.py`; test `tests/test_state_db.py`.

**Interfaces, produced:**
- `acquire_run_lock(plan_id) -> bool`. Returns True when it inserts the row, or when the row names the same plan and `os.getpid()`. A row with another pid, or a NULL pid, is refused.
- `run_lock_info() -> dict | None`, with keys `plan_id`, `pid`, `acquired_at`.
- `open_attempts(contract_ids: list[str]) -> list[dict]`.
- `interrupt_attempt(attempt_id: int) -> None`. Finishes the attempt with outcome `interrupted` and no failure class; does nothing to an attempt that already finished.
- `attempts_for(contract_id: str, role: str | None = None) -> list[dict]`.
- `latest_checkpoint(contract_id, role: str | None = None)`. With a role, it joins checkpoints to attempts by `attempt_id`.
- `StateDB(path)` sets `PRAGMA busy_timeout=10000`.

**Steps:**
- [ ] Write failing tests:
  - `test_run_lock_is_process_exclusive`: insert a row with pid 1, and a row with a NULL pid; both are refused, and the message data comes from `run_lock_info`.
  - `test_open_attempts_are_closed_as_interrupted`
  - `test_attempts_for_filters_by_role`
  - `test_latest_checkpoint_by_role`
  - `test_busy_timeout_is_set`
- [ ] Watch them fail. Implement. Run `.venv/bin/pytest -q tests/test_state_db.py`: all pass. Commit `feat(state): pid-aware run lock, attempt queries, checkpoints by role`.

### Task 2: TEST gets its own scratch; BUILD retries read BUILD notes

**Files:** modify `src/ateam/runner/run.py` and `src/ateam/cli.py` (the `brief` command); test `tests/test_run.py` and `tests/test_cli.py`.

**Interfaces, produced:** `prepare_scratch(paths: TargetPaths, contract_id: str, role: Role = Role.build) -> Path`. It returns `scratch_root/<id>` for build and investigate, and `scratch_root/<id>-test` for test. Both sit at the same depth, so `build_policy`'s home derivation is unchanged. The investigate caller (`runner/investigate.py`) keeps the default.

**Behaviour:** `run_contract_attempt` uses the role's scratch. A BUILD attempt's `prev` checkpoint is `latest_checkpoint(cid, role="build")`; a TEST attempt's is `latest_checkpoint(cid)`, which carries facts only (TEST briefs already drop notes).

**Steps:**
- [ ] Write failing tests:
  - `test_test_role_scratch_is_separate_from_build`
  - `test_test_attempt_never_sees_build_handoff`: a `handoff.yaml` in the BUILD scratch is not in the TEST policy's scratch or its brief.
  - `test_build_retry_brief_uses_build_notes`
  - `test_brief_uses_role_scratch`
- [ ] Implement, run `tests/test_run.py tests/test_cli.py tests/test_policy.py`, and commit `feat(runner): separate TEST scratch; BUILD retries read BUILD notes`.

### Task 3: Machine-readable results; verify `--base`

**Files:** modify `src/ateam/cli.py`; test `tests/test_cli.py`.

**Interfaces, produced:**
- `ateam worker run ... --result-file PATH` writes JSON: `{"code": int, "outcome": "completed|scope_breach|failed|setup_failed|not_binding|refused", "subtype": str|null, "error": str|null, "cost_usd": float, "session_id": str|null, "test_outcome": str|null, "test_class": str|null, "report": obj|null}`. Exit codes are unchanged; a setup failure stays exit 1 with `outcome: setup_failed`.
- `ateam verify C-xxxx --cwd W [--base SHA] [--result-file PATH]` writes `{"code": int, "status": "verified|failed|refused", "failure_class": str|null, "reasons": [str]}`. A refusal (no checks, uncommitted work, not binding) is `status: refused` and never `failed`. `--base` replaces `default_base` for the tree, scope and checkpoint.
- `ateam contracts refresh ... --result-file PATH` writes `{"code": int, "promoted": [ids], "drifted": [ids]}`.

**Steps:**
- [ ] Write failing tests:
  - `test_worker_run_result_file`: covers completed, setup_failed and not_binding, using the existing fakes.
  - `test_verify_base_scopes_against_the_given_commit`: a dependency merged into the base is not a scope breach with `--base`, and is a breach without it.
  - `test_verify_result_file_marks_refusals`
  - `test_refresh_result_file`
- [ ] Implement, then run `tests/test_cli.py`. Every existing test passes unchanged. Commit `feat(cli): --result-file for worker run, verify, refresh; verify --base`.

### Task 4: Integrate helpers the scheduler reuses

**Files:** modify `src/ateam/integrate.py`; test `tests/test_integrate.py`.

**Interfaces, produced:**
- `prepare_integration(ctx, plan_id: str, target_tip: str, echo) -> GitRepo`. This is today's `_prepare`, renamed and made public.
- `reconcile_contracts(db, owner: GitRepo, cs: dict, cids: list[str], branch: str, echo) -> None`. This is today's `_reconcile_contracts`, made public.
- `reconcile_feature(paths, db, plan, echo) -> str`, made public.
- `merge_contract(ctx, wt: GitRepo, plan_id: str, cid: str, echo) -> str`. It merges the latest checkpoint head with no-ff (skipping the merge when the head is already on HEAD), commits the contract file `integrated`, then moves the store `verified -> integrated`, and returns the merge sha. It tolerates a file that already says `integrated`. A conflict raises `MergeConflict` with nothing recorded.

`integrate()` calls these helpers, not copies of them.

**Steps:**
- [ ] Write failing tests:
  - `test_merge_contract_records_and_tolerates_a_repeat`
  - `test_merge_contract_conflict_records_nothing`
  - `test_prepare_integration_is_idempotent`
- [ ] Run `tests/test_integrate.py tests/test_merge.py tests/test_integrate_cli.py`: all pass. Commit `refactor(integrate): public helpers for the scheduler`.

### Task 5: Contract worktrees

**Files:** create `src/ateam/worktrees.py`; test `tests/test_worktrees.py`.

**Interfaces, produced:**
- `@dataclass ContractWorktree(cid: str, path: Path, base: str, branch: str)`
- `contract_worktree(paths, db, owner: GitRepo, plan_id: str, cid: str) -> ContractWorktree`. It records `base:<cid>` from `rev_parse("ateam/<plan_id>")` only when absent, then creates the branch `ateam/<cid>` at that base and the worktree at `<home>/worktrees/<cid>`, or reuses an existing worktree whose branch matches and whose HEAD descends from the base.
- `WorktreeError(Exception)`, raised for a path that isn't a worktree, is on the wrong branch, or doesn't descend from the base.

**Steps:**
- [ ] Write failing tests:
  - `test_cut_from_integration_tip_with_base_recorded`
  - `test_rerun_reuses_recorded_base_not_new_tip`
  - `test_dependent_cut_after_merge_contains_dependency`
  - `test_wrong_branch_or_foreign_history_refused`
- [ ] Implement, run, commit `feat: contract worktrees under ATeam's home`.

### Task 6: Retry policy (pure, table-driven)

**Files:** create `src/ateam/retry.py`; test `tests/test_retry.py`.

**Interfaces, produced:**
- `@dataclass StepOutcome(stage: Literal["build","test","verify"], code: int, outcome: str, subtype: str | None, test_class: str | None, verify_status: str | None, failure_class: str | None)`
- `classify(o: StepOutcome) -> str`. Returns one of `ok`, `implementation`, `environment`, `requirements`, `tamper`, `not_binding`, `refused`, `scope_breach`.
- `@dataclass Decision(action: Literal["continue","retry","pause"], tier: str, reason: str | None)`
- `decide(cls: str, build_attempts_used: int, attempts_allowed: int, tier: str, escalated: bool, last_subtype: str | None, scope_breaches: int) -> Decision`
- `next_tier(tier) -> str | None`, which uses `ORDER`.

**Table (each row a test):**
- BUILD exit 3 → `scope_breach`, retried. Repeated until exhausted → pause `scope_exceeded`.
- `outcome setup_failed` → `environment`.
- `subtype error_max_turns` with no report → `implementation`, escalation evidence.
- `error_max_budget_usd` → `implementation` without escalation.
- A transport error or no report with another subtype → `environment`.
- TEST exit 5 → `test_class` (`requirements` pauses `decision_change_needed` immediately; `implementation` is retried).
- Verify `failed/implementation` → `implementation`; `failed/environment` → `environment`; `failed/tamper` → `tamper`, which pauses `verification_unresolvable`.
- Verify `refused` → `refused`, which pauses `decision_change_needed` naming the reason. It is never retried.
- `not_binding` (exit 4) pauses `records_drifted`.
- Exhausted → per class, as in Global Constraints.

**Steps:**
- [ ] Write failing tests:
  - `test_classify_table`
  - `test_decide_table`
  - `test_escalation_once_and_never_above_careful`
  - `test_pause_reasons_are_the_spec_enum`
- [ ] Implement, run, commit `feat: retry policy`.

### Task 7: The scheduler

**Files:** create `src/ateam/scheduler.py`; modify `src/ateam/config.py` (`run_budget_usd: float = 10.0`); test `tests/test_scheduler.py`.

**Interfaces, produced:**
- `class RunRefused(Exception)`
- `@dataclass RunSummary(plan_id: str, outcome: Literal["merge_proposed","paused","stalled","interrupted","rerun"], contracts: dict[str, str], spent_usd: float, attempts: int, messages: list[str], proposal: Path | None = None)`
- `class Gate`. `shared()` and `exclusive()` are context managers; the gate is writer-preferring.
- `Launch = Callable[[str, str, Path, str, str | None, Path], dict]`, taking (cid, role, worktree, base, tier_override, result_file) and returning the parsed result-file dict.
- `default_launch`, which runs `ateam worker run` as a subprocess in the same process group.
- `run_child(argv) -> dict`, which runs verify or refresh with `start_new_session=True` and reads the result file.
- `run_plan(ctx_factory: Callable[[], Context], plan_id: str, echo, stop: threading.Event, launch: Launch = default_launch, dry_run: bool = False) -> RunSummary`

**Algorithm:**
1. **Preflight**, under the run lock; any failure raises RunRefused and changes nothing:
   - The plan file and the store are `plan_approved`, `executing` or `integrating`, after `reconcile_feature`.
   - The intent repo is clean.
   - Every contract has a frozen hash equal to its `content_hash()`, no record drift (drafts included), a store row, and at least one check of its own.
   - An `integrating` plan with an unfinished contract is refused, naming the state.
   - A paused plan is refused, naming its reason. Other states are refused, naming the state.
2. **Dry run:** print the dependency waves, the tier and model per role, and the worst case (`attempts × (build usd + test usd)` per contract, plus the total against `run_budget_usd`). Return without writing.
3. **Start:** close stale open attempts of the plan's contracts as `interrupted`. Then, under the exclusive gate: walk `plan_approved -> executing` with `set_plan_status`, `prepare_integration`, and `reconcile_contracts`.
4. **Loop** until nothing is runnable, or stop is set, or a pause is pending:
   - Under exclusive, run `refresh` as a child.
   - Pick `ready` and `failed` contracts in `dependency_order`, up to `max_workers`, minus the running ones, if the budget allows the next session.
   - Each pick runs `pipeline(cid)` in a thread:
     - **Worktree** under exclusive.
     - **Setup** under shared.
     - **BUILD** under shared through `launch`.
     - **TEST** under shared.
     - **Verify** under exclusive, with `--base`.
     - **Merge** on `verified`, under exclusive, with `merge_contract`.
     - After each step, `classify` and `decide`: retry → back to BUILD (with the tier override when escalating, and the failing TEST evidence as the prompt); pause → record the pending pause; continue → next step.
   - Echo is serialised with a lock: one line per state change (`cid role outcome $cost`).
5. **Pause:** stop launching, join the running threads, then under exclusive `set_plan_status(["paused"], why=<reason>)`.
6. **Stall:** nothing is runnable and some contracts are unfinished, so the outcome is `stalled`, naming the blockers.
7. **Final step:** when every contract is integrated, `integrate()`. `IntegrateRefused` becomes `rerun` with the message; a pause stays paused.
8. **Interrupt:** stop is set, so launch nothing, wait for the threads (children got SIGINT from the terminal), and close their open attempts as `interrupted`. The plan stays `executing`.
9. **Always:** release the lock.

**Steps:**
- [ ] Write failing tests, using the fake `launch`, which commits real files in the worktree and writes attempt rows through a fresh `StateDB`. The two-contract fixture is A, then B, which depends on A, plus an independent C.
  - `test_preflight_refusals_and_lock` (A1)
  - `test_dry_run_writes_nothing_and_shows_worst_case`
  - `test_happy_path_merges_at_verify_and_stops_at_merge_proposed`: B contains A's files and the target branch is untouched.
  - `test_max_workers_overlap_and_no_session_during_exclusive` (Review Focus 3)
  - `test_scope_breach_retries_then_pauses_scope_exceeded`
  - `test_error_max_turns_escalates_once`
  - `test_requirements_pauses_decision_change_needed`
  - `test_budget_stops_launching_and_pauses`
  - `test_merge_conflict_pauses_environment_failure_contract_verified`
  - `test_stop_interrupts_closes_attempts_and_rerun_continues` (Review Focus 2)
  - `test_rerun_by_state`
  - `test_crash_between_contract_commit_and_store_is_reconciled`
  - `test_stall_names_blockers`
- [ ] Implement, run `.venv/bin/pytest -q tests/test_scheduler.py`, commit `feat: scheduler`.

### Task 8: `ateam run`, docs, spec deviations

**Files:** modify `src/ateam/cli.py`, `README.md`, `docs/first-run-walkthrough.md` and the spec (owner-decision bullets: three deviations plus today's rulings); test `tests/test_run_command.py`.

**Interfaces, produced:** `ateam run PLAN_ID [--dry-run] [--target]`. `run_plan` runs in a worker thread, and the main thread waits. The first SIGINT sets stop and prints "stopping after the running steps; Ctrl-C again to exit now". A second SIGINT exits 130 at once. Exit codes are as in Global Constraints.

**Steps:**
- [ ] Write failing tests:
  - `test_run_exit_codes`
  - `test_dry_run_command`
  - `test_first_interrupt_sets_stop`: drive the handler function directly, not a real signal.
- [ ] Implement, update the docs, then run the full suite: the baseline plus the new tests pass. Commit `feat(cli): ateam run; docs and spec`.
- [ ] Add the opt-in live check `tests/live/test_run_live.py` (`ATEAM_LIVE_TESTS=1`): one light-tier contract, `ateam run`, interrupt during BUILD, and assert that the attempt is `interrupted`. It is not run by default.
