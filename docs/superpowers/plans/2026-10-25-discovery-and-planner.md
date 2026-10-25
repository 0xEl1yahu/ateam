# Discovery and Planner Implementation Plan (pass two, stages 1 and 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two hand-written steps of the pass-one loop. `ateam init` discovers a small baseline of records from a target with evidence and classification, and `ateam plan "<request>"` turns a feature request into contracts with dependencies, acceptance criteria and a three-dimension allocation, plus any records the components need. `ateam approve P-0001` is the first human gate: it walks the proposed records, applies tier overrides, freezes the contracts and moves the feature to `plan_approved`.

**Architecture:** Both stages are INVESTIGATE workers with their own system prompt and report schema (`DISCOVERY_SPEC`, `PLANNER_SPEC`), run through the existing clean-room runner and policy **in the owner's checkout**, under enforced read-only permissions (no editing tools in the session, writes denied by callback and hook, a Bash allow-list with no interpreters) and a before-and-after snapshot of the checkout that marks the attempt `tamper` if anything changed. No worktree is created for a role that cannot write. The coordinator does all validation and writing: it turns candidates into `proposed` records (fingerprint dedupe, dead-glob rejection, global cap), turns a planner report into `draft` contracts and a `Plan` file (dependency and check validation, tier rules), commits in the intent repo, drives the feature state machine, and prints a review the owner approves from. Nothing a worker says becomes binding without the owner.

**Tech Stack:** unchanged. One new packaged data file, `src/ateam/data/rubric.md`.

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`, sections "Discovery", "Allocation", "State machines", "Contracts", "Intent repo", and the 2026-10-25 owner decisions.

## Global Constraints

- All pass-one and intent-repo constraints hold: work in a worktree, test first, clean-room sessions, no `rm` for workers, nothing written into the target, durable intent only in the intent repo, commits there with the `ateam:` prefix and a clean-repo guard.
- Plan ids match `^P-\d{4}$`. Feature statuses and transitions are `state/db.py`'s `FEATURE_TRANSITIONS`.
- Discovery and planning workers are INVESTIGATE: session tools `Read, Glob, Grep, Bash` only; every write denied by the permission callback and the PreToolUse hook (their scratch directory is the sole exception, for the handoff file); Bash limited to the read-only allow-list in the spec's Roles table, with no interpreter, `sed` or `awk`; running in the owner's checkout with a snapshot of `git status --porcelain`, HEAD and `git stash list` taken before and compared after. A difference is recorded as `tamper` and the report is discarded.
- Enforcement is pre-guard first: tool list, then callback and hook, then the post-run snapshot. The system prompt explains the role and is never relied on.
- A candidate record or contract that fails validation is reported and dropped; it is never saved in a half-valid state. Nothing proposed is approved by code.
- Live tests stay opt-in (`ATEAM_LIVE_TESTS=1`), haiku alias, `max_budget_usd<=0.5` per session.

## Review Focus

- An INVESTIGATE session that tries `python -c`, `sed -n 'w f'`, `awk '{print > "f"}'`, `tee`, a redirect, or any editing tool is denied before execution, and an investigation after which the checkout's status, HEAD or stash list differs is recorded as `tamper` with its report discarded. (Tasks 3 and 4)
- A discovery candidate whose glob matches no file in the target is dropped and named in the report, never saved. (Task 5)
- A candidate whose fingerprint equals a `rejected` record's fingerprint is not re-proposed, and the report says so. (Task 5)
- A planner report with a dependency cycle, a dependency index out of range, or a check name absent from config is refused with a message naming the contract, and nothing is written to the intent repo or the state store. (Task 6)
- `ateam approve P-0001` raises a TEST tier that is below the BUILD tier to the BUILD tier and says so in its output; it never lowers one. (Task 7)
- `ateam plan` on a target with no approved records succeeds, and the planner brief's Binding section states that no records apply. (Task 6)

---

## File structure

```
src/ateam/plans/__init__.py
src/ateam/plans/model.py         Plan, PlanStatus, PlanHistoryEntry
src/ateam/plans/store.py         load_plans(), save_plan(), next_id("P")
src/ateam/allocation.py          Level maps, tier_for_complexity(), investigate_tier_for(), test_tier_for(), check_allocation()
src/ateam/data/rubric.md         the one-page rubric the planner reads and the owner can edit
src/ateam/runner/policy.py       INVESTIGATE allow-list without interpreters, sed or awk (modify)
src/ateam/runner/snapshot.py     CheckoutSnapshot: status, HEAD, stash list; take(), changed()
src/ateam/roles.py               + DISCOVERY_SPEC, PLANNER_SPEC (INVESTIGATE tools, own prompts and schemas)
src/ateam/runner/investigate.py  run_investigation(): an in-place INVESTIGATE session with snapshot and telemetry
src/ateam/discovery.py           candidates_to_records(), DiscoveryResult, render_discovery_report()
src/ateam/planner.py             report_to_plan(), PlanResult, render_plan_review()
src/ateam/cli.py                 + init, plan, approve P, reject P, records review, plans show|list
examples/plan.example.yaml
tests/test_plan_model.py, tests/test_allocation.py, tests/test_policy.py (additions), tests/test_snapshot.py, tests/test_investigate.py,
tests/test_discovery.py, tests/test_planner.py, tests/test_cli.py (additions), tests/live/test_init_plan_live.py
```

---

### Task 1: Plan model and store

**Files:** Create `src/ateam/plans/__init__.py`, `src/ateam/plans/model.py`, `src/ateam/plans/store.py`, `tests/test_plan_model.py`, `tests/test_plan_store.py`

**Interfaces:**
- `plans.model.PlanStatus` StrEnum with the feature statuses from `FEATURE_TRANSITIONS` (`requested, investigating, plan_proposed, plan_approved, plan_rejected, executing, paused, integrating, merge_proposed, merged, abandoned`).
- `plans.model.PlanHistoryEntry(at: datetime, by: str, to: PlanStatus, why: str | None = None)`.
- `plans.model.Plan(id: str = Field(pattern=r"^P-\d{4}$"), title: str, request: str, status: PlanStatus = requested, contracts: list[str] = [], proposed_records: list[str] = [], unknowns: list[str] = [], tier_overrides: dict[str, dict[str, str]] = {}, history: list[PlanHistoryEntry] = [])` with `slug()` from `title` (same rule as contracts) and `content_hash()` excluding `status` and `history`.
- `plans.store.PlanStoreError`, `load_plans(dir) -> dict[str, Plan]`, `save_plan(dir, plan) -> Path`, `next_id(dir, prefix="P")` (reusing the records store conventions and `_FILE_RE`).

- [ ] Write failing tests: `test_plan_defaults_and_slug`, `test_content_hash_ignores_status_and_history`, `test_roundtrip_next_id_and_mismatch` (mirrors the contract store tests).
- [ ] Run → FAIL (import). Implement. Run → PASS. Commit: `feat: plan model and store`.

### Task 2: Rubric and allocation rules

**Files:** Create `src/ateam/data/rubric.md`, `src/ateam/allocation.py`, `tests/test_allocation.py`

**Interfaces:**
- `data/rubric.md`: one page. For each dimension, three rows (Low, Medium, High) with the repository signals the planner must cite: component boundaries crossed, approved records bound, test coverage of touched paths, migrations, concurrency, security surface, ambiguity of the request. States explicitly that file count alone is not a signal, and gives the three mappings below in a table.
- `allocation.tier_for_complexity(level: Level) -> str`: low→`light`, medium→`standard`, high→`careful`.
- `allocation.investigate_tier_for(uncertainty: Level) -> str`: low→`none`, medium→`light`, high→`standard`.
- `allocation.test_tier_for(risk: Level, build_tier: str) -> str`: low→`at_least(build_tier, "light")`, medium→`at_least(build_tier, "standard")`, high→`careful`.
- `allocation.default_tiers(a: Allocation) -> dict[str, str]`: the three functions applied.
- `allocation.check_allocation(a: Allocation) -> list[str]`: human-readable problems: unknown tier names; `test` below `build` (returns the corrected value in the message); `investigate` is `none` while uncertainty is high; empty `reasoning`.
- `allocation.rubric_text() -> str` reads the packaged file.

- [ ] Write failing tests: `test_default_tiers_mapping` (nine combinations), `test_check_allocation_flags_test_below_build`, `test_check_allocation_flags_none_investigate_on_high_uncertainty`, `test_rubric_text_mentions_three_dimensions_and_no_file_count`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: allocation rubric and tier rules`.

### Task 3: Read-only enforcement for in-place investigations

**Files:** Modify `src/ateam/runner/policy.py`; Create `src/ateam/runner/snapshot.py`; Test `tests/test_policy.py`, `tests/test_snapshot.py`

**Interfaces:**
- `policy.INVESTIGATE_ALLOWED_PREFIXES` becomes exactly: `git log`, `git show`, `git diff`, `git blame`, `git status`, `git ls-files`, `git grep`, `git branch --list`, `git branch -a`, `git branch --show-current`, `git rev-parse`, `git stash list`, `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `find`, `tree`, `du`, `stat`, `file`, `pwd`, `sort`, `uniq`, `cut`. No `python`, `python3`, `sed`, `awk`, `xargs`, `env`, `sh`, `bash`. The `_WRITE_OPS` check for INVESTIGATE stays (redirects, `tee`, `-o`, `--output`, `-fprint`).
- `snapshot.CheckoutSnapshot` dataclass `(head: str, porcelain: list[tuple[str, str]], stashes: int)`; `snapshot.take(git: GitRepo) -> CheckoutSnapshot`; `snapshot.changed(before, after) -> list[str]` (human-readable differences: head moved, N paths changed status, stash count changed).

- [ ] Write failing tests. Policy: a parametrised deny table for INVESTIGATE: `python -c "open('x','w')"`, `python3 -m pytest --collect-only`, `sed -n 'w out.txt' f`, `awk '{print > "f"}' f`, `cat f | tee out`, `ls > out`, `find . -fprint out`, `env X=1 ls`, `xargs rm`, `bash -c ls`; an allow table: `git --no-pager log -5`, `git stash list`, `git branch --list`, `find src -name '*.py'`, `cat src/app.py | head`. Snapshot: `test_take_and_unchanged`, `test_changed_reports_head_paths_and_stash` (commit, create an untracked file, `git stash push -u` in a temp repo).
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: strict read-only allow-list and checkout snapshot for investigations`.

### Task 4: Investigation specs and runner

**Files:** Modify `src/ateam/roles.py`; Create `src/ateam/runner/investigate.py`, `tests/test_investigate.py`

**Interfaces:**
- `roles.DISCOVERY_SPEC: RoleSpec` (role `investigate`, INVESTIGATE tools, prompt: read `CLAUDE.md`, `AGENTS.md`, `README*`, `docs/**`, configuration, tests and `git log` for the last 200 commits; propose at most `max_candidates` records; for each, classify `origin` (documented only when quoting a doc, CLAUDE.md, AGENTS.md or a commit message), `basis` (observation when the only evidence is code), cite evidence as `path[:line]`, `path::test`, or `commit <sha>`; bind the narrowest glob the evidence supports; separate what the code does from what it is meant to do; list unknowns). Schema `DISCOVERY_SCHEMA`: `{candidates: [{kind, statement, rationale, origin, basis, evidence: [{type, ref, note?}], paths: [str], serves: [str], confidence}], unknowns: [str], notes}`, `additionalProperties: false`.
- `roles.PLANNER_SPEC: RoleSpec` (role `investigate`; prompt: given the request, the approved records index, the config checks and the rubric, produce contracts that are each independently verifiable, with declared paths including the test files a BUILD worker must write, acceptance criteria with `test` checks naming `path::test` inside the contract's paths or `judgment` checks that cite a record, `depends_on` as indices into the same list, and an allocation per contract with the three levels, cited signals in `reasoning`, and tiers following the rubric; propose records only for decisions the plan itself introduces; list unknowns rather than guessing). Schema `PLANNER_SCHEMA`: `{title, contracts: [{title, paths, incidental, depends_on: [int], acceptance: [{id, statement, check: {type, ref?}}], checks: [str], allocation: {complexity, risk, uncertainty, reasoning, tier: {build, test, investigate}}, budget?: {attempts, usd_per_attempt, max_turns}}], proposed_records: [candidate], unknowns: [str], notes}`.
- `runner/investigate.py`: `InvestigationResult(res: WorkerResult, attempt_id: int, tampered: list[str])`; `async run_investigation(*, paths, config, target_git, name: str, spec: RoleSpec, prompt: str, brief_text: str, tier: TierSpec, db: StateDB) -> InvestigationResult`: runs **in the owner's checkout** (`cwd=paths.root`); `prepare_scratch(paths, name)`; builds an INVESTIGATE `Policy` with `worktree=paths.root`, `allowed_paths=[]`, `scratch_dir`, `ateam_home`; takes `snapshot.take(target_git)` and the intent-repo head/cleanliness before; `start_attempt(contract_id=name, role="investigate", ...)` with predicted levels `{}`; runs `run_worker`; compares both snapshots after; outcome `tamper` (failure class `tamper`, report set to `None`) when either changed, else `completed` when a report exists, else `failed`; always closes the attempt on exceptions (same try/except as `run_contract_attempt`).

- [ ] Write failing tests (fake `query`): `test_discovery_and_planner_specs_are_investigate_and_strict` (tools exactly `Read, Glob, Grep, Bash`), `test_run_investigation_runs_in_checkout_and_records_attempt` (options `cwd` equals the target root; no worktree or branch created), `test_run_investigation_marks_tamper_when_checkout_changes` (the fake query writes an untracked file into the target), `test_run_investigation_marks_tamper_when_intent_changes`, `test_run_investigation_closes_attempt_on_exception`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: discovery and planner specs; investigation runner`.

### Task 5: Discovery and `ateam init`; `ateam records review`

**Files:** Create `src/ateam/discovery.py`, `tests/test_discovery.py`; Modify `src/ateam/cli.py`, `tests/test_cli.py`

**Interfaces:**
- `discovery.DiscoveryResult(proposed: list[Record], dropped: list[tuple[dict, str]], skipped_rejected: list[str], unknowns: list[str], capped: int)`.
- `discovery.candidates_to_records(candidates: list[dict], unknowns: list[str], existing: dict[str, Record], repo_root: Path, records_dir: Path, cap_global: int, by: str = "ateam-init") -> DiscoveryResult`: for each candidate in order: build `Evidence`s, compute the fingerprint; skip when it equals the fingerprint of any existing record (rejected ones counted in `skipped_rejected`, others silently); validate every glob with `is_valid_pattern` and `expand(...)` non-empty, else drop with reason `dead glob <g>`; a candidate with `origin: documented` and no doc-type evidence is downgraded to `inferred` (noted in `rationale` suffix "(downgraded: no documentary evidence)"); candidates with code-only evidence get `basis: observation` regardless of what the worker said; allocate ids with `next_id` continuing across the batch; cap globals at `cap_global` (extra globals dropped with reason `global cap`); `status=proposed`, history entry by `by`.
- `discovery.render_discovery_report(result: DiscoveryResult) -> str`: proposed records grouped by kind with id, origin/basis, statement, evidence refs, paths; dropped with reasons; skipped as previously rejected; unknowns; the next command (`ateam records review`).
- CLI `ateam init [--remote URL] [--max-candidates N (default 12)] [--tier light|standard|careful (default standard)]`: `intent init` if missing; runs `run_investigation` with `DISCOVERY_SPEC`; brief = render of the existing approved records index plus the cap; writes the proposed records; `commit_intent(paths, "ateam: propose N records from discovery", ["records"])`; prints the report; exit 0 even with zero candidates.
- CLI `ateam records review [--all]`: iterates `proposed` records (default) in id order; for each prints the record and prompts `approve [r]equirement / approve [o]bservation / re[j]ect / [s]kip / [q]uit`; reject asks for a reason; applies through `RecordService`; prints a summary. Non-TTY input works through click's prompt (tested with `CliRunner(input=...)`).

- [ ] Write failing tests: `test_candidates_dead_glob_dropped_and_reported`, `test_rejected_fingerprint_not_reproposed`, `test_documented_without_doc_evidence_downgraded`, `test_code_only_evidence_is_observation`, `test_global_cap`, `test_ids_continue_from_existing`, `test_render_report_lists_next_command`; CLI with monkeypatched `run_investigation` returning a canned report: `test_init_writes_proposed_records_and_commits`, `test_init_with_zero_candidates_exits_zero`, `test_records_review_approve_reject_skip_via_input`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: discovery (ateam init) and records review`.

### Task 6: Planner and `ateam plan`

**Files:** Create `src/ateam/planner.py`, `tests/test_planner.py`; Modify `src/ateam/cli.py`, `tests/test_cli.py`; Create `examples/plan.example.yaml`

**Interfaces:**
- `planner.PlanResult(plan: Plan, contracts: list[Contract], proposed: DiscoveryResult, problems: list[str], bound_preview: dict[str, list[str]])`.
- `planner.PlanError(Exception)` carrying `problems: list[str]`.
- `planner.report_to_plan(report: dict, request: str, plan_id: str, contracts_dir: Path, records: dict[str, Record], config: TargetConfig, repo_root: Path, records_dir: Path, by: str = "ateam-plan") -> PlanResult`: validates before allocating anything: every `depends_on` index in range and acyclic (Kahn), every `checks` name present in `config.checks`, every path glob valid, every `test`-type acceptance ref's file part matching one of the contract's paths (problem, not fatal: appended to `problems` as a warning), allocation via `check_allocation` with `test` raised through `test_tier_for` and `investigate` set by `investigate_tier_for` when the planner left it `none` on medium or high uncertainty (both recorded in `problems` as notes); any fatal problem raises `PlanError` with the full list and nothing written. Otherwise allocates contract ids in order, maps indices to ids, builds `Contract`s with `status=draft` and `plan=plan_id`, `checks` resolved to `NamedCheck`s from config, builds the `Plan`, and runs `candidates_to_records` for `proposed_records` with `by`. `bound_preview[contract_id]` is `select_records(contract.paths, graph, repo_root)` ids.
- `planner.render_plan_review(result: PlanResult, tiers) -> str`: the plan header; one block per contract with paths, dependencies, acceptance criteria, checks, the three levels, the reasoning, the tier per role with model names, the bound records preview; proposed records (reusing the discovery report renderer); unknowns; problems/notes; the approval command with its options.
- CLI `ateam plan "<request>" [--tier ...]`: `next_id("P")`, `db.transition("feature", P, "requested", by="owner")` then `investigating`; brief = request, approved records index (id, kind, statement, binds), config checks, rubric text, tier names; runs `run_investigation` with `PLANNER_SPEC`; `report_to_plan`; saves contracts and plan and proposed records; `commit_intent(..., f"ateam: propose plan {P}", ["plans", "contracts", "records"])`; transition to `plan_proposed`; prints the review. On `PlanError`: prints the problems, transitions the feature to `plan_rejected` with reason `planner output invalid`, exit 1.
- CLI `ateam plans list`, `ateam plans show P-0001`.
- `examples/plan.example.yaml` annotated; `tests/test_examples.py` gains `test_plan_example_loads`.

- [ ] Write failing tests: `test_report_to_plan_happy_path_ids_deps_tiers`, `test_dependency_cycle_refused_nothing_written`, `test_out_of_range_dependency_refused`, `test_unknown_check_refused`, `test_test_tier_raised_to_build_noted`, `test_investigate_tier_set_on_high_uncertainty`, `test_plan_without_approved_records_ok`, `test_render_review_contains_allocation_and_command`; CLI with monkeypatched `run_investigation`: `test_plan_command_writes_plan_contracts_and_transitions`, `test_plan_command_invalid_report_rejects_and_writes_nothing`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: planner (ateam plan)`.

### Task 7: Plan approval and rejection

**Files:** Modify `src/ateam/cli.py`, `src/ateam/plans/model.py` if needed; Test `tests/test_cli.py`

**Interfaces:**
- CLI `ateam approve P-0001 [--tier C-0002=careful ...] [--tier C-0002:test=careful] [--skip-records]`: requires status `plan_proposed`; unless `--skip-records`, runs the records review over the plan's `proposed_records` (interactive, same prompts as Task 5); applies tier overrides to the named contracts (whole contract or one role), re-running `check_allocation` and raising TEST to BUILD with a printed note; freezes every contract (`select_records` + `freeze` + `save_contract` + `upsert_contract` + record hashes) and commits `ateam: approve plan P-0001` with `["plans", "contracts"]`; records `tier_overrides` and a history entry on the plan; transitions the feature to `plan_approved`; prints what was frozen, in dependency order, with ready/draft status.
- CLI `ateam reject P-0001 "reason"`: status `plan_proposed` → `plan_rejected`; plan history gets the reason; contracts stay `draft` on disk (they are never frozen); commit `ateam: reject plan P-0001`.
- `ateam status` shows plans with their status and contract counts.

- [ ] Write failing tests: `test_approve_freezes_contracts_in_dependency_order_and_transitions`, `test_approve_applies_tier_override_and_raises_test_tier`, `test_approve_requires_plan_proposed`, `test_approve_runs_records_review_unless_skipped`, `test_reject_plan_records_reason_and_leaves_contracts_draft`, `test_status_lists_plans`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: plan approval and rejection`.

### Task 8: Docs and live tests

**Files:** Modify `README.md`, `docs/first-run-walkthrough.md`, `examples/CLAUDE.target.md`, `CLAUDE.md`; Create `tests/live/test_init_plan_live.py`

- [ ] README: the five setup steps become `ateam intent init`, `ateam init`, `ateam records review`, `ateam plan "<request>"`, `ateam approve P-0001`, then the existing worker run; "Using it for development today" updated: planning and discovery are automated, scheduling/verification/integration still manual; command list extended; plan example linked.
- [ ] Walkthrough: section 2 becomes `ateam init` on the playground (expected: a couple of proposed records with evidence, likely `inferred`/`observation`), `ateam records review` with the keystrokes, then `ateam plan "Add a health function that returns ok"`, the printed review, `ateam approve P-0001`; the hand-written YAML stays as an appendix for repos where you want to seed records directly.
- [ ] `examples/CLAUDE.target.md` and `CLAUDE.md` mention the new commands where they list commands.
- [ ] `tests/live/test_init_plan_live.py` (opt-in): on the `cli_target` repo plus a README with one explicit rule, `ateam init` proposes at least one record whose evidence cites the README; `ateam plan "add a health function"` yields at least one contract whose paths include `src/**` and whose allocation levels are valid; cost printed; under 0.10 dollars total.
- [ ] `ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live -s` → PASS. `.venv/bin/pytest -q` → PASS. Commit: `docs: init, plan and approve in README and walkthrough; live tests`.
