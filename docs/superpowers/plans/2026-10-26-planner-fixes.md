# Planner fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plans come back revised when their review finds important problems, read-only sessions stop wasting turns on a handoff file they cannot write, interrupted planning never strands a plan at `investigating`, and contract references in a stored plan name real contract ids.

**Architecture:** Small changes in four places:
- the `plan` command in `src/ateam/cli.py`: the revision trigger, interrupt handling and stale-plan cleanup;
- the INVESTIGATE prompt in `src/ateam/roles.py` and the investigation briefs in `src/ateam/brief.py`;
- one feature edge in `src/ateam/state/db.py`;
- a reference rewrite in `src/ateam/planner.py`.

**Tech Stack:** Python 3.14, click, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md` ("Decisions made by the owner": "Plan, review, hand back"; "State machines"). The owner's rulings of 2026-10-26 below update that bullet and the diagram.

## Global Constraints

- **Revision trigger (owner, 2026-10-26):** a review with verdict `needs_revision` and at least one `blocking` or `important` finding earns exactly one revision round. There is no second review, and the stored review keeps `revised: true`.
- **Handoff (owner):** INVESTIGATE sessions (init, plan, review, revision) are not told to update `handoff.yaml`, and their briefs carry no "Handoff file" section. BUILD and TEST keep theirs. No write path for investigators.
- **Interrupted planning (owner):**
  - New feature edge `investigating -> plan_rejected`.
  - `ateam plan` records it with reason `interrupted` when interrupted (Ctrl-C).
  - It also records it, with reason `interrupted (found stale)`, for every other `investigating` plan whose planning process is gone. A process counts as gone when its `planning:<plan-id>` meta pid is absent or not alive.
  - Each rejected plan gets a committed plan file, like the existing invalid-report path.
  - The ref snapshot is unchanged and documented: don't create branches or worktrees while planning.
- **Contract references (implementer's mechanic):** the planner refers to other contracts of its own plan as `contract N` (zero-based). `report_to_plan` rewrites every `contract N` with a valid N to its assigned id in every string of the report before building contracts and the plan.
- **Unchanged:** the CLAUDE.md conventions; never `monkeypatch.undo()`; in `cli_target`, C-0001 is the seed fixture.

## Review Focus

1. **A second terminal planning at the same time.** Its live `investigating` plan must never be rejected as stale.
2. **Ctrl-C during the review session.** That path already saves the plan unreviewed, and it must keep doing so. Only an interrupt before the plan exists rejects it.
3. **`contract 12` in a 3-contract plan, and `contract` in ordinary prose** (for example "the contract's checks"). Neither may be rewritten.
4. **A revision whose report is unusable.** The first valid plan is kept, and its review is stored with `revised: false`, as today.

---

### Task 1: Revision on blocking or important findings

**Files:** modify `src/ateam/cli.py` (around line 456); test `tests/test_cli.py`.

**Steps:**
- [ ] Change the existing test (around line 962), which asserts an important-only review is not revised, into `test_plan_important_review_triggers_one_revision`: one revision runs, and `revised` is True.
- [ ] Add `test_plan_minor_only_review_is_not_revised`.
- [ ] The revision prompt starts "A staff-engineer review found problems with your plan." Update the assertion in `test_plan_blocking_review_triggers_one_revision`.
- [ ] Watch the tests fail, implement, run `tests/test_cli.py -k review`, and commit `feat(plan): revise once on blocking or important findings`.

### Task 2: No handoff instruction for read-only sessions

**Files:** modify `src/ateam/roles.py` (`INVESTIGATION_COMMON`) and `src/ateam/brief.py` (`assemble_investigation_brief`, `assemble_review_brief`); test `tests/test_brief.py` and `tests/test_roles.py`.

**Steps:**
- [ ] Write failing tests:
  - `test_investigation_and_review_briefs_have_no_handoff_section`
  - `test_investigation_prompt_does_not_ask_for_a_handoff_file`
  - `test_build_and_test_briefs_keep_the_handoff_file` (a guard)
- [ ] Implement, run `tests/test_brief.py tests/test_roles.py tests/test_investigate.py`, and commit `fix(investigate): read-only sessions are not asked to write handoff.yaml`.

### Task 3: Interrupted planning is recorded

**Files:** modify `src/ateam/state/db.py` (the edge, plus `features_with_status(status) -> list[str]`) and `src/ateam/cli.py` (the `plan` command); test `tests/test_state_db.py` and `tests/test_cli.py`.

**Behaviour:**
- `plan` sets the meta key `planning:<pid>` to `os.getpid()` after `investigating`, and deletes it on every exit.
- A `KeyboardInterrupt` before the plan is written calls `_rejected("interrupted")` and exits 130.
- At start, for every feature in `investigating` other than the new one, whose marker is absent or names a pid that is not alive (`os.kill(pid, 0)` raises `ProcessLookupError`), `plan` writes and commits a rejected plan file and moves the feature `investigating -> plan_rejected`, with why `interrupted (found stale)`. It prints `P-xxxx -> plan_rejected (interrupted, found stale)`.
- `_rejected` uses the new direct edge.

**Steps:**
- [ ] Write failing tests:
  - `test_investigating_can_be_rejected` (db)
  - `test_plan_interrupted_before_the_plan_is_written_is_rejected`
  - `test_plan_cleans_up_a_stale_investigating_plan`
  - `test_plan_leaves_a_live_planning_session_alone` (marker set to `os.getpid()`)
  - `test_ctrl_c_during_review_still_saves_the_plan_unreviewed` (the existing behaviour, as a guard)
- [ ] Implement, run `tests/test_cli.py tests/test_state_db.py`, and commit `feat(plan): interrupted planning is rejected, never stranded at investigating`.

### Task 4: `contract N` becomes the contract id

**Files:** modify `src/ateam/planner.py` and `src/ateam/roles.py` (one sentence in `PLANNER_SPEC`); test `tests/test_planner.py`.

**Interfaces, produced:** `resolve_contract_refs(value, ids: list[str])`. It walks dicts, lists and strings recursively and replaces `\bcontract (\d+)\b`, case-insensitively, when the index is in range, keeping anything out of range as written.

**Steps:**
- [ ] Write failing tests:
  - `test_contract_refs_are_rewritten_to_ids`: the interface notes, acceptance statements and unknowns all say `C-xxxx`.
  - `test_out_of_range_and_prose_are_left_alone` (Review Focus 3)
  - `test_planner_prompt_explains_contract_n`
- [ ] Implement, run `tests/test_planner.py tests/test_roles.py`, and commit `feat(planner): contract N references become contract ids`.

### Task 5: Docs and the full suite

- [ ] Spec:
  - Update the "Plan, review, hand back" bullet (blocking or important; one round; no second review).
  - Add the `investigating -> plan_rejected` edge to the diagram.
  - Add one owner bullet covering the handoff and interrupt rulings.
- [ ] README step 3 ("Blocking findings earn one revision round"), and the walkthrough review paragraph.
- [ ] Run `.venv/bin/pytest -q`. Expected: the baseline plus the new tests passed. Commit `docs: planner fixes`.
