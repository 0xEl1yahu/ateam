# Bootstrap integrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, by hand, the three pieces ATeam needs to carry a multi-contract plan from approval to the target branch: `ateam contracts refresh`, `ateam integrate P-xxxx` and `ateam merge P-xxxx`.

**Architecture:**
- **Git:** git primitives go in `gitops.py`.
- **Proposal:** a pure proposal module renders the merge report and parses its header.
- **Integrate:** an integrator module does preflight and reconciliation, records the integration start, prepares the integration worktree, merges contracts, merges in a moved target, runs the final checks and commits the proposal.
- **Merge:** a merge module is the fast-forward gate in the owner's checkout.
- **CLI and store:** the CLI commands are thin wrappers. The state store gains two feature edges and stores the integration start as a meta key.
- **Ordering:** the intent repo is always written first, then the store.

**Tech Stack:** Python 3.14, click, pydantic, SQLite (`StateDB`), git CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`, sections "State machines" and "Verification and integration", plus the owner decisions of 2026-10-25 and 2026-10-26. The integrator request and its review are in P-0013 and P-0014 (`ateam plans show`); they are reference only and are never approved.

## Global Constraints

- **Git-native only:** no `.git/info/exclude`, no local git config writes, no push, no fetch, no rebase. ATeam never sets a git identity.
- **No deletion:** nothing runs `rm`. Integration worktrees and branches are left in place after merge or reject.
- **Fast-forward only:** merge fast-forwards only, in the owner's checkout. If a fast-forward is impossible, it refuses; no merge commit and no rebase.
- **Moved target:** a moved target is merged into the integration branch (`git merge --no-ff <target tip>`), never rebased.
- **Commit ordering:** intent commits use the `ateam:` prefix and stage only the files written (`commit_intent_files`). The intent repo is written first, then the store.
- **Porcelain:** never `str.strip()` porcelain output.
- **Test fixtures:** in `cli_target`, C-0001 is the seed fixture. Plans made in tests start at C-0002. End-to-end contracts touch disjoint files.
- **Scoped patches:** never `monkeypatch.undo()`; use `monkeypatch.context()`.
- **Pause reasons** come from the spec enum: `environment_failure` for a conflict or a setup failure, and `verification_unresolvable` for failed final checks or tamper.
- **Exit codes:**
  - `integrate`: 0 merge_proposed, 1 checks or setup failed (paused), 3 conflict (paused), 4 refused.
  - `merge`: 0 merged, 1 fast-forward impossible, 4 refused.
  - `contracts refresh`: 0, or 1 when any contract drifted.

## Review Focus

1. **A crash between the intent commit and the store write.** A re-run must replay exactly the edge integrate itself makes, and refuse anything else.
2. **A reused integration worktree with a merge in progress or dirty files.** Expected: a refusal naming the state, never a merge on top.
3. **A target that moved after the proposal.** `merge` refuses with both commits named. Re-running `integrate` refreshes the proposal.
4. **An edited, uncommitted proposal file.** `merge` trusts only `HEAD:reports/<plan>-merge.md` and refuses a dirty intent repo.
5. **A dependent contract whose bound record drifted.** `refresh` leaves it draft, names the record, and exits 1.

---

### Task 1: Git primitives

**Files:**
- Modify: `src/ateam/gitops.py`
- Test: `tests/test_gitops.py` (tests already written: `test_rev_parse_branch_exists_create_branch`, `test_is_ancestor`, `test_merge_no_ff_makes_a_merge_commit`, `test_merge_conflict_aborts_and_names_paths`, `test_merge_ff_only`, `test_worktree_add_existing_and_show_file`; they fail now)

**Interfaces:**
- Produces:
  - on `GitRepo`:
    - `rev_parse(ref) -> str`: the full sha of `ref^{commit}`; GitError when unknown.
    - `branch_exists(name) -> bool`
    - `create_branch(name, start) -> None`
    - `is_ancestor(a, b) -> bool`: `merge-base --is-ancestor`; exit 1 means False, other errors raise GitError.
    - `merge_no_ff(rev, message) -> str`: returns the new HEAD.
    - `merge_ff_only(rev) -> str`
    - `merge_in_progress() -> bool`
    - `worktree_add_existing(path, branch) -> None`
    - `show_file(rev, relpath) -> str`
    - `diff_stat(a, b) -> str`
  - in the module: `class MergeConflict(GitError)` with `paths: list[str]`.

- [ ] Run `.venv/bin/pytest -q tests/test_gitops.py`: 6 failed.
- [ ] Implement the primitives. `merge_no_ff` runs `merge --no-ff --no-edit -m`. On any failure while MERGE_HEAD exists, it collects the unmerged paths (`diff --name-only --diff-filter=U`), runs `merge --abort`, and raises MergeConflict when paths exist, else GitError.
- [ ] Run the tests: all pass.
- [ ] Commit: `feat(gitops): primitives for integrate and merge`.

### Task 2: State edges, shared plan helpers, `contracts refresh`

**Files:**
- Modify: `src/ateam/state/db.py`, `src/ateam/cli.py`, the spec (State machines diagram plus an owner bullet for each edge)
- Create: `src/ateam/plans/ops.py`
- Test: `tests/test_state_db.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - Feature edges: `FEATURE_TRANSITIONS["integrating"] == {"merge_proposed", "paused"}` and `FEATURE_TRANSITIONS["merge_proposed"] == {"merged", "plan_rejected", "integrating"}`.
  - In `ateam/plans/ops.py`:
    - `plan_contract_ids(db, plan) -> list[str]`
    - `dependency_order(contracts: dict) -> list[str]`, moved from cli; cli imports both.
  - In cli, `set_plan_status(ctx, plan, steps: list[str], by, why=None, extra_files=()) -> Plan`. It saves the plan file at the last step and commits that file plus `extra_files` in one `ateam:` commit, then makes each store transition in `steps`.
- [ ] Failing tests:
  - `test_integration_edges` (db table).
  - `test_refresh_promotes_a_draft_whose_dependencies_are_verified`. Use a two-contract approved plan; set C-0002 verified through the store and the file. Assert that C-0003's file and store say `ready`, its hash is unchanged, there is one intent commit, and the output shows `C-0003 -> ready`.
  - `test_refresh_keeps_a_draft_with_an_unverified_dependency`: the output names C-0002, and the status stays draft.
  - `test_refresh_stops_on_drift`: the bound record is edited. Assert that the contract stays draft, the output names the record, and the exit code is 1.
  - `test_refresh_skips_contracts_of_dead_plans`.
- [ ] Implement:
  - `ateam contracts refresh [PLAN_ID]`. It covers draft contracts with a frozen hash whose plan is `plan_approved`, `executing` or `integrating`, and whose dependencies are all `verified` or `integrated` in the store.
  - Drift means the contract stays draft and is reported; otherwise the file is set to `ready`.
  - One commit `ateam: refresh <ids>`, then `transition draft -> ready`.
  - Status only: records are not re-selected and the hash is unchanged.
- [ ] Run `.venv/bin/pytest -q tests/test_state_db.py tests/test_cli.py -k "refresh or edges or supersede"`: all pass.
- [ ] Commit: `feat: contracts refresh, integration feature edges, shared plan helpers`.

### Task 3: Merge proposal

**Files:**
- Create: `src/ateam/proposal.py`
- Test: `tests/test_proposal.py`

**Interfaces:**
- Produces:
  - `proposal_relpath(plan_id) -> str`, which returns `"reports/<plan_id>-merge.md"`.
  - `@dataclass ProposalHeader(plan_id: str, branch: str, tip: str, target_branch: str, target_tip: str)`.
  - `render_proposal(header, *, diff_stat: str, contracts: list[dict], records: list[str], risks: list[str], telemetry: list[dict]) -> str`. It writes a YAML header between `---` lines, then the sections "Diff summary", "Evidence per contract", "Records", "Unresolved risks" and "Telemetry".
  - `parse_header(text) -> ProposalHeader`, raising `ProposalError(ValueError)` on a missing or malformed header.
- [ ] Failing tests:
  - `test_round_trip`: `parse_header(render_proposal(h, ...)) == h`, and every section heading is present.
  - `test_parse_rejects_missing_or_bad_header`.
  - `test_render_lists_judgment_criteria_as_risks`: a risk string appears in the output.
- [ ] Implement, run the tests, commit: `feat: merge proposal render and parse`.

### Task 4: Integrator

**Files:**
- Create: `src/ateam/integrate.py`
- Test: `tests/test_integrate.py`, driven through the CLI from Task 6 where convenient (write the CLI wrapper here if the tests need it)

**Interfaces:**
- Consumes: Task 1 primitives; Task 2's `plan_contract_ids`, `dependency_order`, `set_plan_status` and edges; Task 3's `render_proposal`, `proposal_relpath` and `ProposalHeader`; `verify.run_checks`, `check_env`, `snapshot` and `compare`; `runner.run.ensure_setup` and `SetupError`; `contracts.select.drift`.
- Produces:
  - `integration_branch(plan_id) -> str` (`"ateam/<plan_id>"`).
  - `integration_worktree(paths, plan_id) -> Path`: `home_dir(root) / "worktrees" / plan_id`.
  - `class IntegrateRefused(Exception)`.
  - `@dataclass IntegrateResult(outcome: Literal["merge_proposed","conflict","checks_failed","setup_failed"], plan_id, tip: str | None, proposal: Path | None, messages: list[str])`.
  - `integrate(ctx, plan_id, echo) -> IntegrateResult`. `ctx` is the cli `Context`, and `echo` prints progress.
  - The meta key `integration:<plan_id>` holds the JSON `{"branch", "start", "target_branch"}`.

The algorithm, in order. Each bullet is a step with its own test.
1. **Preflight**, which raises IntegrateRefused:
   - The plan exists.
   - The feature is `plan_approved`, `executing`, `integrating` or `merge_proposed` in the store; `paused` is refused, naming the reason from its last transition.
   - The intent repo is clean.
   - Every contract in `plan_contract_ids` is verified or integrated in both its file and the store, has `content_hash() == db.contract_hash`, has no drift, and has a checkpoint with a `head`.
   - The check set (config.checks plus every contract's checks, de-duplicated by `(name, cmd)`) is not empty.
2. **Reconcile.** If a contract's file says integrated, its store says verified, and its checkpoint head is an ancestor of the integration branch, replay `verified -> integrated`. If the plan file status is ahead of the store along `plan_approved -> executing -> integrating -> merge_proposed`, replay the store steps. Any other mismatch is refused.
3. **Merge-proposed shortcut.** If the feature is merge_proposed and the target tip is an ancestor of the branch tip, return merge_proposed with "already proposed". If the target moved, `set_plan_status(..., ["integrating"])`.
4. **Walk.** Use `set_plan_status` up to `integrating`.
5. **Start and branch.** Record the meta start (the target tip) if it is absent, before creating the branch. If the branch is missing, create it at the recorded start. If it exists but does not descend from the start, refuse.
6. **Worktree.** If missing, `worktree_add_existing`. If present, require that `current_branch` is the integration branch, that `status_porcelain` is empty and that no merge is in progress; otherwise refuse, naming the state. Then `ensure_setup`. On SetupError, pause with `environment_failure` and return setup_failed.
7. **Contract merges**, in dependency order. Skip integrated contracts. Otherwise `merge_no_ff(head, "ateam: integrate <cid> into ateam/<plan>")`, then set the contract file to integrated (one commit `ateam: integrate <cid>`), then `transition verified -> integrated` with the reason `merged <sha>`. On MergeConflict, pause with `environment_failure: <cid> conflicts in <paths>` and return conflict.
8. **Moved target.** If the target tip is not an ancestor of HEAD, `merge_no_ff(target_tip, "ateam: merge <target> into ateam/<plan>")`. A conflict pauses as in step 7.
9. **Final checks.** Take a snapshot over the plan's contract fingerprints (a sha256 of the concatenated `db.fingerprint(cid)` values), run the checks, and compare. On a failure or tamper, pause with `verification_unresolvable: <names>`, and print the check tails (last 20 lines) through echo. Return checks_failed.
10. **Proposal.** Render it with `diff_stat(target_tip, tip)`; the evidence from each checkpoint's checks and warnings; the records (the plan's proposed records with their current status, plus every contract's bound record ids); the risks (plan unknowns, checkpoint warnings, and judgment and command criteria not checked by minimal verify); and the telemetry (the attempts of the plan's contracts with outcome, cost and turns). Write it, then `set_plan_status(["merge_proposed"], extra_files=[proposal])`.

- [ ] Failing tests in `tests/test_integrate.py`, using a fixture that builds two approved, verified contracts C-0002 and C-0003 on disjoint files, with check `unit: true`, verified through `ateam verify`:
  - `test_happy_path_reaches_merge_proposed`: the branch has both merge commits, both contracts are integrated in file and store, the proposal is committed with a parsable header whose tip equals the branch tip, and the feature is merge_proposed.
  - `test_refuses_unverified_contract_and_paused_feature`
  - `test_conflict_pauses_with_environment_failure`: the contract stays verified, and the paths are named.
  - `test_failing_final_check_pauses`
  - `test_moved_target_is_merged_in`
  - `test_rerun_after_moved_target_refreshes_proposal`
  - `test_reconcile_replays_contract_and_feature_steps`: simulate the file ahead of the store.
  - `test_reused_worktree_with_merge_in_progress_is_refused`
  - `test_recorded_start_without_branch_recreates_branch`
  - `test_setup_failure_pauses`
- [ ] Implement `integrate.py`; run `.venv/bin/pytest -q tests/test_integrate.py`: all pass.
- [ ] Commit: `feat: ateam integrate`.

### Task 5: Merge gate

**Files:**
- Create: `src/ateam/merge.py`
- Test: `tests/test_merge.py`

**Interfaces:**
- Consumes: Task 1 (`merge_ff_only`, `is_ancestor`, `rev_parse`, `show_file`, `branch_exists`), Task 3 (`parse_header`, `proposal_relpath`), Task 2 (`set_plan_status`), Task 4 (`integration_branch`).
- Produces:
  - `class MergeRefused(Exception)`
  - `class FastForwardImpossible(MergeRefused)`
  - `@dataclass MergeResult(plan_id, target_branch, old_head, new_head)`
  - `merge_plan(ctx, plan_id) -> MergeResult`

The gate:
- **Refusals:**
  - The feature is not merge_proposed. The one exception is file `merged` with store `merge_proposed` and the target already at the tip, where the store step is replayed.
  - The intent repo is dirty.
  - The owner's checkout is not on `config.target_branch`, or it is dirty (untracked files count).
  - The header from `intent.show_file("HEAD", proposal_relpath)` does not match the plan, or its branch is not `integration_branch(plan_id)`.
  - The branch tip is not the header's tip.
- **Fast-forward impossible:** if the target is not an ancestor of the tip, raise FastForwardImpossible naming both shas.
- **Merge:** if the target is not already equal to the tip, run `merge_ff_only(tip)`. Then `set_plan_status(["merged"])`.

- [ ] Failing tests:
  - `test_merge_fast_forwards_and_marks_merged`
  - `test_refuses_wrong_branch_or_dirty_checkout`
  - `test_refuses_edited_uncommitted_proposal`
  - `test_refuses_tip_mismatch`
  - `test_fast_forward_impossible_changes_nothing`
  - `test_rerun_after_interruption_completes_without_ref_change`
- [ ] Implement, run the tests, commit: `feat: ateam merge (fast-forward only)`.

### Task 6: CLI, reject of merge_proposed, docs

**Files:**
- Modify: `src/ateam/cli.py`, `README.md`, `docs/first-run-walkthrough.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 4's `integrate`, `IntegrateRefused` and `IntegrateResult`; Task 5's `merge_plan`, `MergeRefused` and `FastForwardImpossible`.
- Produces:
  - `ateam integrate PLAN_ID [--target]` and `ateam merge PLAN_ID [--target]`, with the exit codes from Global Constraints.
  - `ateam reject` also accepts merge_proposed in both file and store.
- [ ] Failing tests:
  - `test_integrate_then_merge_end_to_end`: exit codes 0 and 0, main equals the proposed tip, and the feature is merged.
  - `test_integrate_and_merge_exit_codes`: 4 when refused, 3 on a conflict, 1 on failed checks; for merge, 4 when refused and 1 when a fast-forward is impossible.
  - `test_reject_merge_proposed_plan`: both branches are untouched and the contracts stay integrated.
- [ ] Implement, then update the README (command list; replace step 5's "Then merge the branch" with the integrate and merge flow) and the walkthrough.
- [ ] Run the full suite: `.venv/bin/pytest -q`. Expected: 1135 plus the new tests passed, 2 skipped, 3 xfailed.
- [ ] Commit: `feat(cli): integrate, merge, reject merge_proposed; docs`.

### Task 7: Live check on ATeam's own intent

- [ ] With the merged build, in the main checkout, run `ateam contracts refresh --help` and `ateam integrate P-0009`. Expected: exit 4, refused because P-0009's contracts are not verified. This confirms the preflight against real data, read-only.
