# ATeam integration audit, 2026-10-29

A read-only audit of the whole repository at `731f536`, made after P-0021 was merged. No code was changed. Four scoped reviewers covered the execution path, the worker-session boundary, the persistence contracts, and dead code with docs drift and test quality. Every important finding was re-checked against the code before it was included.

**How each finding was checked:**

- **Reproduced:** a reviewer wrote a small throwaway script against a scratch copy (a probe) and saw the failure happen.
- **Confirmed:** the code was re-read and does what the finding says.
- **Static:** a reviewer read the code; it was not re-checked independently.

## Answer: does it work as one coherent system?

**Yes, the main path works as one system. The weak points are the edges: crash recovery, keeping the TEST worker hands-off, and the record-binding gate.**

Evidence that the main path works:

- **ATeam built its own last four plans.** P-0018 to P-0021 were planned, approved, run, verified, integrated and merged by ATeam itself.
  - That was 25 contracts integrated: 75 BUILD/TEST attempts plus 2 investigations, $21.32 in total.
  - All 47 contracts in the state store match their YAML files on status, plan and content hash. All 21 plans agree too.
- **Unit suite:** 2010 passed, 10 skipped, 3 xfailed in 285.68 s (exit 0).
- **The run's sub-commands are wired correctly.** `ateam run` launches each step as a separate `ateam` command (BUILD and TEST workers, verify, refresh, integrate, pause-investigate). All five command lines the scheduler builds were fed through the real CLI option parser, and all parsed. The keys each step writes to its result file match the keys the scheduler reads (`scheduler.py:762-830`).
- **The Claude SDK settings are real.** Every option ATeam passes exists in the installed SDK (0.2.174), and every SDK field it reads exists there too. The bundled CLI accepts the clean-room flags.

What is **not** shown:

- **The event logging from P-0021 has never run for real.** No `logs/` folder exists under the ATeam home. The live tests were last run on 2026-10-27, before P-0021 changed how `run_worker` writes events.
- **No unit test runs the safety policy the way a real worker session wires it.** A worker run calls the SDK; the policy is attached there as a hook (`hooks`) and a permission callback (`can_use_tool`). The tests swap in fake SDK sessions and fake workers that never call either.

## A. Architecture

- **One command-line tool.** `cli.py` holds all the commands. A run is executed by `scheduler.py`, which launches each step as a child `ateam` command.
- **Two records of state.** The private intent repo (YAML files in git) is the source of truth: records, contracts, plans, reports and guidance. The SQLite state store (`state.db`) follows it with statuses, attempts, checkpoints, pauses and the run lock. The code always writes the intent commit first and the store second.
- **The worker boundary** is `runner/`:
  - `options.py` is the only place session options are built.
  - `policy.py` is the safety check that runs before every tool call.
  - `run.py` holds attempts and the scope check; `investigate.py` and `snapshot.py` handle read-only sessions.
- **Verify, integrate and merge** (`verify.py`, `integrate.py`, `merge.py`) work on real git worktrees.

The structure matches the design spec. Where it doesn't, the cause is unfinished or stale documentation (section D), not a second competing system.

## B. Integration status

| Workflow | Status | Evidence |
|---|---|---|
| `plan`, review, `approve` (freezes contracts) | Partial | Works (4 real plans), but finding 3 lets a hand-edited approved record be bound, and finding 7 has a crash window. |
| `run`: lanes, BUILD/TEST launch, verify, retry | Connected | The command-line and result-key checks above; the real `verify` runs as a child process in the tests. |
| Merge at verify onto `ateam/P-xxxx` | Partial | Conflicts are handled. Any other git error crashes the run (finding 9). |
| Conflict, rebase BUILD, then TEST and verify again | Connected | Rebase tests pass. |
| Pause, investigate, `status`, `resume`, `stop` | Connected / Partial | Wired. `resume` has crash windows (finding 8). |
| Final `integrate`, merge proposal, `ateam merge` | Connected | Outcome mapping at `scheduler.py:576-583`; fast-forward only. |
| Crash recovery on re-run | Partial | Two crash windows wedge the plan (findings 7 and 8). |
| Worker BUILD session | Connected | Clean room set on every session path; policy fails closed (any exception denies). |
| Worker TEST session | Partial | Findings 1 and 2. |
| Event logs into `log`, `status`, `watch` | Unverified live | The code is wired; no real run has written a log yet. |

## C. Contract debt register

| # | Contract | Producer → consumer | Expected vs actual | Evidence | Check |
|---|---|---|---|---|---|
| C1 | TEST role separation | TEST session → verify | **Expected:** TEST may not edit source (spec:346). **Actual:** only the `Write` tool is denied. `sed -i`, `cat > f <<EOF` and `git commit -am` are allowed, and the after-session check is only the path scope. | `runner/run.py:254-265` | Policy probe reproduced; post-check confirmed |
| C2 | TEST verdict coverage | TEST report → `classify_test_report` | **Expected:** a verdict for every criterion (spec:379). **Actual:** an empty `verdicts` list counts as `completed`. | `run.py:182-186` | Confirmed and reproduced |
| C3 | BUILD report `status` | worker → run and retry logic | The schema requires `done\|blocked\|failed`, and no code reads it. A blocked BUILD still goes on to TEST and verify. | `roles.py:78,101`, `run.py:265` | Confirmed |
| C4 | Record selection scope | contract → `select_records` | The rule is "`paths` plus `incidental` everywhere". **Actual:** selection uses `paths` only, so records bound to an incidental path are never in the brief. | `cli.py:849`, `cli.py:1585`, `planner.py:408` vs `run.py:133` | Confirmed and reproduced |
| C5 | Record hash at approve and freeze | records → `record_hashes` | **Expected:** an edited approved record is refused. **Actual:** approve and freeze save the new hash, which also erases the "hand edit" warning. | `cli.py:882-885`, `cli.py:1595-1596` | Reproduced |
| C6 | Plan status walk | `set_plan_status` → `db.transition` | The plan file is committed before the store checks the move is legal, so an illegal status stays in the intent repo. | `plans/ops.py:45-47` | Confirmed and reproduced |
| C7 | Unknown cost | session → budget and telemetry | **Expected:** an unknown cost counts at the tier cap. **Actual:** it becomes $0 on Ctrl-C and when the SDK's `total_cost_usd` is `None`. | `cli.py:115-117`, `run.py:105,114` | Reproduced (Ctrl-C), static |
| C8 | `worker run` exit code | CLI → scripts and the owner | Exits 0 on tamper. The scheduler is unaffected because it checks the outcome first. | `cli.py:2172-2178` | Static |
| C9 | Brief budget | contract → brief | The brief prints the contract's budget, but the session enforces the tier caps (`options.py:53-54`). | `brief.py:93` | Static |
| C10 | `ateam merge` exit code | CLI → README | Exits 4 in a case where the README says 1. | `merge.py:77-80` | Static |

## D. Technical debt

- **A second shell runner.** Setup commands use `subprocess.run(shell=True, timeout=…)` with no process group (`run.py:171`). A timed-out setup leaves its child processes running in the worktree; a probe reproduced this with surviving `sleep`s. `verify.py:80-94` already solved this with `start_new_session` plus `killpg`.
- **The store has no migrations.** `SCHEMA_VERSION` is written but never compared (`db.py:117-124`). The first time a column is added, existing homes get an `OperationalError`.
- **Content hashes break when a field is added.** They cover the whole model dump (`contracts/model.py:105-108`, `records/model.py:79-82`). Adding any field with a default makes every frozen contract look hand-edited.
- **Nothing checks routinely that YAML and the store agree.** `contracts validate` compares hashes only. It reports a false drift on integrated C-0078, because R-0019 was retired after it ran. `doctor` checks neither.
- **Repeated logic:**
  - the ATeam home path is computed in 5 places (`paths.py:54,104`, `policy.py:1126`, and `.parent.parent.parent` at `run.py:133`, `investigate.py:48`);
  - "`paths` plus `incidental`" is assembled by hand at 6 places;
  - `rebase_conclusion_problem` reimplements the scope check (`scheduler.py:1109-1128`).
- **Dead in production:**
  - `records/store.py:59 file_hash`;
  - `gitops.worktree_remove` and `is_ignored`;
  - `events.parse_event_ref`;
  - `retry.PAUSE_REASONS` (never enforced);
  - `StateDB.schema_version`;
  - `scheduler.RUNNABLE`;
  - the contract status `paused`, which is never written, so its checks are dead;
  - ruff `F401` at `cli.py:16` and `F541` at `proposal.py:75`.
- **Settings that are accepted and never read:**
  - `serialize_checks` (`config.py:25`, documented in the example and the spec);
  - `Plan.tier_overrides`;
  - `Record.binds.incidental`;
  - the reports' `proposals` and `confidence` fields;
  - the contract's `budget.max_turns` and `usd_per_attempt`.
- **An orphan attempt.** Attempt 20, an INVESTIGATE for P-0016 started on 2026-10-26, was never closed. That plan's ID sits in the `contract_id` column. `ateam status` hides it, but bare `ateam watch` would follow it through `running_attempts()` (`db.py:359`) and block forever.
- **Documentation drift:**
  - **README:** "Not yet automated" still lists the event logs, `watch` and the TEST role. The Layout section omits `logs/`, `runs/`, `worktrees/` and `guidance/`. The Commands list omits `verify` and `pause-investigate` and several flags. It names "the full verifier (plan P-0002)", but P-0002 was rejected. "A few cents" undersells the live suite.
  - **Walkthrough:** line 7 says the same three things are not built.
  - **Spec:** its CLI section (spec:453-468) lists commands that don't exist (`telemetry`, `approve --reject-record`, …). It still marks three things "pass two", and it describes a third enforcement layer that doesn't exist (settings deny rules).

## E. Test and runtime findings

**What was run:**

- **`.venv/bin/pytest -n auto`:** 2010 passed, 10 skipped, 3 xfailed, exit 0. The 3 xfails are strict and match residual gaps the owner accepted; the skips depend on the environment.
- **Ruff** (F, B, E9 and PLE rules; the repo has no ruff config):
  - in `src`, 6 findings: 2 real but trivial (the two in section D), 3 `zip(strict=)` style points in `policy.py`, and the B023 at `scheduler.py:596`, which is a false positive because the closure is called within the same loop iteration;
  - in `tests`, 78 findings, all benign: 51 are fixture re-imports.
- **The reviewers:** 221 targeted scheduler, integrate and merge tests, 31 worker tests and 61 more, all passing.
- **Real data:**
  - `records validate`: 0 errors, 4 warnings (dead globs on R-0018 and R-0020).
  - `contracts validate`: exit 1 on the false C-0078 drift.
  - Both were run against a copy of the ATeam home.
- **Not run:** live tests (they use paid API calls); any type checker (none is installed).

**Weak spots in the tests:**

- **Fakes at the key joins.** Worker CLI tests replace `run_contract_attempt` with a fake, and scheduler tests replace `default_launch` with `FakeWorkers` (`test_scheduler.py:54-98`).
- **No guard against accidental paid sessions.** Only `default_investigate` raises if a unit test forgets to fake it (`conftest.py:176-183`). If a unit test forgets to fake the SDK `query` or `default_launch`, it can start a real, paid session.
- **Sleep-based timing.** `test_scheduler.py:531,550,619` rely on a `sleep(0.8)` beating a 0.5 s poll, the same flaky pattern as backlog X9.

**Smallest set of tests that would add real confidence:**

1. `default_launch` against the real `worker run` with a contract that doesn't bind. Check the result keys, exit 4 and the outcome classification. No API call.
2. `run_contract_attempt` with a fake `query` that actually calls the hook and the permission callback. Deny `rm` and an out-of-scope write; allow a write to an `incidental` path.
3. Autouse guards that make `query` and `default_launch` raise unless a test overrides them.
4. A regression test for each crash and contract fix below.

**Limitation:** two `ateam status` calls against the real home ran `init_schema()`. That is a `CREATE TABLE IF NOT EXISTS` plus a WAL pragma; no data changed.

## F. Prioritized remediation

There is **no P0**. No core workflow is broken and no exploitable security hole was found.

Size is in scope (S = a few lines plus a test, M = one module, L = several modules), not time. **Owner** means CLAUDE.md reserves the decision for the owner.

| ID | Pri | Issue | Evidence | Impact | Fix | Size |
|---|---|---|---|---|---|---|
| 1 | P1 | TEST can edit and commit source through the shell | C1 | The builder/tester split can be bypassed | Snapshot HEAD and the dirty tree around TEST; mark a change `tamper`. **Owner** | S |
| 2 | P1 | TEST verdicts not checked against the criteria | C2 | Judgment criteria can go unjudged | Treat a missing or unknown id as not met / cannot determine. **Owner** (changes "done") | S |
| 3 | P1 | Approve and freeze bind hand-edited approved records | C5 | Rule 5 bypassed silently, and the evidence erased | Refuse when the stored hash differs; only the record commands write hashes | S |
| 4 | P1 | Record selection ignores `incidental` | C4 | Binding records missing from the brief | Pass `[*paths, *incidental]` at all 3 call sites | S |
| 5 | P1 | Deletion through `unlink`, `shred`, `rmdir`, `os.remove` is allowed | `policy.py:806-810` | An ordinary path around rule 3 ("Important" by CLAUDE.md's grading) | Extend `_is_rm` and add table rows. **Owner** (narrows the policy) | S |
| 6 | P1 | Refresh crash, then `reconcile_contracts` crashes with a TypeError | `integrate.py:289`, `cli.py:1652-1655` | The plan is wedged on every re-run | Read the checkpoint lazily; replay ready over draft | S |
| 7 | P1 | Approve crash window leaves the plan stranded | `cli.py:875-885` | Every command refuses the plan | Do the store writes in one transaction; replay from the committed approval | M |
| 8 | P1 | An interrupted verify can't be recovered from `run` | `cli.py:2394-2422`, reconcile refusal | The run refuses with no hint of the cure | Skip the disagreement when a verify-pending marker exists; name the command to run | S |
| 9 | P1 | Non-conflict git errors crash the run | `scheduler.py:921`, `gitops.py:148-160` | Traceback, no pause record, plan left `executing` | Map GitError to an `environment_failure` pause; add `-c rerere.enabled=false` | S |
| 10 | P2 | BUILD `blocked`/`failed` ignored | C3 | Wasted attempts and the wrong failure class | Map it to failed or requirements. **Owner** (state machine) | S |
| 11 | P2 | `set_plan_status` commits an illegal walk | C6 | An illegal status stays durably in the intent repo | Check the walk before committing | S |
| 12 | P2 | Setup runner leaves processes behind | `run.py:171` | Strays keep changing the worktree | Reuse verify's process-group runner | S |
| 13 | P2 | Standalone `integrate`, `verify` or `refresh` during a live run; second Ctrl-C releases the lock while children run | `cli.py:985,2256,1599,1077` | Two writers, false tamper reports | Tell the children they belong to the run; refuse others. **Owner** (gate) | M |
| 14 | P2 | No store migrations; field-sensitive hashes | section D | The first schema change breaks existing homes and contracts | Version check plus ordered `ALTER`s; hash a fixed set of fields | M |
| 15 | P2 | No YAML/store agreement check; false C-0078 drift | section D | Divergence found only at run time | Extend `contracts validate` | S |
| 16 | P2 | Test gaps at the session and scheduler joins; no guard against paid sessions; live tests stale | section E | Wiring regressions go unnoticed | Tests 1–4 above, plus one live run | M |
| 17 | P2 | `serialize_checks` is never read | `config.py:25` | A documented guarantee isn't delivered | Implement it or remove it. **Owner** | S |
| 18 | P2 | The budget is per run, not per plan | `scheduler.py:465` | Total spend per plan has no limit; the README implies otherwise | Decide the meaning, then fix the code or the docs. **Owner** | S |
| 19 | P3 | Unknown or interrupted cost recorded as $0 | C7 | Spend understated | Use `None`, counted at the tier cap | S |
| 20 | P3 | Orphan attempt 20; `watch` follows it forever | `db.py:359` | `watch` shows a window that never ends | Close planning attempts when a stale plan is rejected | S |
| 21 | P3 | Exit codes and brief caps | C8–C10 | Misleading to people and scripts | Fix the code or the docs | S |
| 22 | P3 | Dead code, unused fields, repeated home/scope logic | section D | Maintenance cost | One `ateam_base()` and one `Contract.scope`; delete the dead symbols | M |
| 23 | P3 | README, walkthrough and spec drift | section D | Misleads the next reader | One docs pass | M |

## G. Recommended order

1. **Crash and recovery fixes (6, 8, 9, 11, 7).** Each is self-contained and test-first, and none needs an owner ruling. They fix actual broken behaviour.
2. **Binding fixes (4, 3).** Both are mechanical and enforce existing rules (rule 5, scope everywhere).
3. **Owner rulings, then the small fixes:** 1, 2, 5, 10, 17, 18, 13.
4. **Tests (16)**, including one live run. That also gives the first real event log, which closes the gap left by P-0021.
5. **Debt (12, 14, 15, 19–22)**, then **docs (23)** last, so they describe the final state.

Items 1–9 and 11 are the size of an ATeam plan, so they could go through `ateam plan` as usual.
