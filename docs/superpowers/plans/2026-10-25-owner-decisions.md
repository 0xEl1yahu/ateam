# Owner Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the ten owner decisions of 2026-10-25 that affect planning and worker runs today, plus the four brief gaps the second dogfood run exposed, so the next generation of plans is deep, non-duplicated, reviewed, and runnable in a fresh worktree.

**Architecture:** Every change is coordinator-side Python or role text. New plan outcomes (`plan_covered`, `superseded`) are plan statuses and feature transitions, recorded in the intent repo and the state store like the existing ones. The staff-engineer review is a second INVESTIGATE session over the draft plan, with one planner revision round when it finds blocking problems. Policy changes are pre-guards with table-driven tests. Nothing here touches the target repo.

**Tech Stack:** Python 3.14, pydantic, click, claude-agent-sdk 0.2.x, pytest. Run everything with `.venv/bin/...`.

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md` (sections "Decisions made by the owner", "State machines", "Roles", "Allocation", "Discovery", "CLI surface"). Task 1 adds the ten decisions to the spec; later tasks argue from them.

## Global Constraints

- Test first: every behaviour change starts with a failing test watched to fail (`CLAUDE.md`, Conventions).
- Enforcement is pre-guard first; role text explains, never controls (spec, owner 2026-10-25).
- Nothing of ATeam's is written into the target; approvals and plan outcomes commit only in the intent repo with the `ateam:` prefix (spec, "Intent repo").
- Workers never run `rm`; INVESTIGATE never writes (spec, Roles).
- Never `str.strip()` porcelain output. Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- This repo is nested in an unrelated parent git repo; confirm `git rev-parse --show-toplevel` ends in `/owner-decisions` (this worktree) before any git command.
- Keep `tests/test_policy.py` table-driven: new deny and allow strings go in the existing tables.
- Spec state machine after Task 1: feature `investigating -> plan_covered` (terminal) and `plan_proposed -> superseded` (terminal) are the only new transitions. No other transition changes.

## Review Focus

1. A `cd` whose target is a symlink inside the worktree pointing outside it, or `cd` to a path that only exists after resolution through `..` (expected: denied; Task 2 tests `cd src/../..` and a symlink).
2. A planner that returns zero contracts and names a plan that is `plan_rejected`, `plan_covered` or the plan being replaced as covering it (expected: fatal "no contracts", not a covered outcome; Task 5 tests all three).
3. `--replaces` naming a plan that is `plan_approved` or later (expected: refused before any session runs; Task 6 tests it).
4. The review session tampers, crashes or returns no report (expected: the plan is still written with `review.verdict == "unavailable"` and a printed warning, never lost; Task 7 tests all three).
5. A setup command that fails in a fresh worktree (expected: `worker run` exits 1 with the command and its output, nothing is recorded as set up, no attempt is started; Task 8 tests it).

---

### Task 1: Record the decisions in the spec and CLAUDE.md

**Files:**
- Modify: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md` ("Decisions made by the owner" list after the `rm` entry at line 93; "State machines" at line 285; "CLI surface" at line 415)
- Modify: `CLAUDE.md` (Conventions)

**Interfaces:**
- Produces: the ten decision bullets below, which Tasks 2-9 implement; the state-machine lines Task 6 encodes.

- [ ] **Step 1: Append ten bullets to "Decisions made by the owner", each ending "(Owner, 2026-10-25.)"**

1. **Investigations default to the careful tier.** `ateam init` and `ateam plan` run at `careful` unless `--tier` says otherwise; depth of reading is worth more than the dollar at planning time.
2. **INVESTIGATE may `cd` inside the worktree.** The target must resolve to an existing directory under the worktree (the owner's checkout for in-place investigations); anything else, including `cd` alone and `cd -`, is denied.
3. **Plan, review, hand back.** Every `ateam plan` runs the planner, then a staff-engineer review of the draft by a fresh INVESTIGATE session; blocking findings earn the planner one revision round; the review travels with the plan to the owner. `--no-review` skips it.
4. **Records for files that do not exist yet** are accepted when a contract in the same plan declares the path; they stay proposed like any other record.
5. **Setup runs once per worktree.** `config.setup` commands run in a worktree before its first attempt, never again for that worktree; a failing setup stops the run.
6. **Failure classification** is the planner's rule (a failed `test`/`command` criterion is `implementation`) plus a `requirements_unclear` flag the TEST worker may raise, which classifies the attempt `requirements`.
7. **`ateam contracts refresh` stops on drift**: it leaves the draft in place and reports; it never rewrites a drifted contract. (Constrains a future feature; implemented with it.)
8. **Rebuild derives only from the intent repo.** Doctor's rebuild recreates state that the intent repo determines and nothing else. (Constrains a future feature; implemented with it.)
9. **Merge is fast-forward only by default, in the owner's checkout.** (Constrains a future feature; implemented with it.)
10. **A request already covered by an existing plan is a `plan_covered` outcome, not a rejection.** `ateam plan --replaces P-xxxx` hides that plan from the planner and marks it `superseded` when the new plan is approved.

- [ ] **Step 2: Update "State machines"**

Add after the feature diagram: `investigating -> plan_covered` and `plan_proposed -> superseded`, both terminal, with one sentence each (covered: the planner returned no contracts and named the covering plans; superseded: a later plan approved with `--replaces`).

- [ ] **Step 3: Update "CLI surface"**

Add `ateam plan "<request>" [--tier careful] [--replaces P-xxxx] [--no-review]`; note `init`'s default tier.

- [ ] **Step 4: CLAUDE.md Conventions: one line**

"INVESTIGATE may `cd` only to a directory inside its worktree; `tests/test_policy.py` holds the allow and deny strings."

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-10-24-ateam-harness-design.md CLAUDE.md
git commit -m "docs: record the ten owner decisions of 2026-10-25 and the two new plan outcomes"
```

---

### Task 2: INVESTIGATE may `cd` inside the worktree

**Files:**
- Modify: `src/ateam/runner/policy.py` (`command_allowed`, around the `Role.investigate` block at line 324; `INVESTIGATE_ALLOWED_PREFIXES` unchanged)
- Test: `tests/test_policy.py` (`INVESTIGATE_MUST_DENY`, `INVESTIGATE_MUST_ALLOW`, `INVESTIGATE_IN_PLACE_DENY`, `INVESTIGATE_IN_PLACE_ALLOW`)

**Interfaces:**
- Produces: `Policy._cd_target(segment: str, cwd: Path) -> Path | None` returning the resolved directory when the segment is `cd <one path>` or `pushd <one path>` with no substitution, else `None`; `command_allowed` tracks a `cwd` across segments of one command, starting at `self.worktree`.

- [ ] **Step 1: Add table rows**

Allow (investigate, worktree-relative): `cd src && ls`, `cd src/ateam; grep -n def runner/run.py`, `cd "$PWD" && ls` is NOT allowed (substitution rule already denies `$`), `pushd tests && ls`, `cd <abs worktree>/src && ls`.
Deny with these exact reasons:
- `cd` alone, `cd -`, `cd ~`, `cd ..`, `cd src/../..`, `cd /tmp && ls`: `"INVESTIGATE may cd only to a directory inside the worktree"`.
- `cd src && cd ../.. && ls`: same reason (cwd tracking: second `cd` escapes).
- `cd .ateam && ls`: existing reason `"changing directory into .ateam/ or .claude/ is not allowed"` (keep its precedence).
- A symlink test in the in-place tables: create `link -> /tmp` inside the fixture root; `cd link && ls` denied with the worktree reason.

- [ ] **Step 2: Run the policy tests to verify they fail**

Run: `.venv/bin/pytest tests/test_policy.py -q -k "investigate"`
Expected: FAIL on the new allow rows (currently denied as "may only run read-only inspection commands") and on the new deny rows' reason text.

- [ ] **Step 3: Implement**

In the `Role.investigate` branch of `command_allowed`, before the allow-list check per segment: if `_cd_target` returns a path, require `path.resolve()` to be an existing directory with `self.worktree.resolve()` among its parents or equal to it, then set `cwd = path` and `continue`; otherwise return the worktree reason. Resolve relative targets against `cwd`. Non-investigate roles keep today's behaviour.

- [ ] **Step 4: Run the whole policy file, then the suite**

Run: `.venv/bin/pytest tests/test_policy.py -q` then `.venv/bin/pytest -q`
Expected: PASS, 432 + new rows.

- [ ] **Step 5: Commit**

```bash
git add src/ateam/runner/policy.py tests/test_policy.py
git commit -m "feat(policy): INVESTIGATE may cd to a directory inside its worktree"
```

---

### Task 3: Careful default, the Read hint, and the rules-scope note

**Files:**
- Modify: `src/ateam/cli.py` (`init` and `plan` `--tier` defaults, lines 180 and 237)
- Modify: `src/ateam/roles.py` (`INVESTIGATION_COMMON`)
- Modify: `src/ateam/runner/policy.py` (the investigate allow-list denial message at line 336)
- Modify: `src/ateam/brief.py` (`_rules`)
- Test: `tests/test_cli.py`, `tests/test_brief.py`, `tests/test_policy.py`, `tests/test_roles.py` (or wherever role text is asserted; `grep -rn INVESTIGATION_COMMON tests/`)

**Interfaces:**
- Produces: `RULES_SCOPE_NOTE = "These are the target's instructions for whoever develops it. They govern how work on the target is done; they are not requirements of the Request."` in `brief.py`, rendered as the first line of `## Rules`.

- [ ] **Step 1: Write the failing tests**

```python
def test_init_and_plan_default_to_careful(monkeypatch, cli_target):
    # reuse the fake run_investigation pattern at tests/test_cli.py:319; capture tier_name
    ...
    assert seen["tier_name"] == "careful"

def test_rules_section_opens_with_scope_note(target_repo, tmp_path):
    text = _inv(target_repo, tmp_path)
    assert _section(text, "Rules").startswith(RULES_SCOPE_NOTE)

def test_investigation_common_tells_workers_to_use_read_for_line_ranges():
    assert "Read tool with offset and limit" in INVESTIGATION_COMMON
```
Policy table: `sed -n 1,5p x.py` for investigate is denied with reason containing `"use the Read tool with offset and limit"`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py tests/test_brief.py tests/test_policy.py -q -k "careful or scope_note or offset"`
Expected: FAIL (default is `standard`; note and hint absent).

- [ ] **Step 3: Implement**

Defaults to `"careful"`. `INVESTIGATION_COMMON` gains: "To read part of a file use the Read tool with offset and limit; sed, awk and interpreters are not available to you." The denial for a segment whose first word is `sed`, `awk`, `python`, `python3`, `perl`, `ruby`, `node` appends `"; use the Read tool with offset and limit to read a line range"`. `_rules` prefixes `RULES_SCOPE_NOTE + "\n\n"`.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (update any test that asserted the old default or exact Rules text).

- [ ] **Step 5: Commit**

```bash
git add src/ateam/cli.py src/ateam/roles.py src/ateam/runner/policy.py src/ateam/brief.py tests/
git commit -m "feat: careful tier by default for investigations; Read hint and rules-scope note in the brief"
```

---

### Task 4: The plan index carries contract contents

**Files:**
- Modify: `src/ateam/brief.py` (`_plans`, `assemble_investigation_brief` signature)
- Test: `tests/test_brief.py`

**Interfaces:**
- Produces: `assemble_investigation_brief(..., exclude_plans: set[str] = frozenset())`; `_plans(plans, contracts, exclude: set[str]) -> str`; `PLAN_INDEX_CAP = 2500` characters per plan.
- Consumed by Task 6 (`exclude_plans={replaces}`) and Task 7 (review brief).

- [ ] **Step 1: Write the failing test**

```python
def test_plan_index_lists_acceptance_unknowns_and_excludes(target_repo, tmp_path, sample_plan, sample_contract):
    text = _inv(target_repo, tmp_path, plans={sample_plan.id: sample_plan}, contracts={sample_contract.id: sample_contract})
    sec = _section(text, "Existing plans and contracts")
    assert f"{sample_contract.id} draft  {sample_contract.title}" in sec
    assert f"    A1: {sample_contract.acceptance[0].statement}" in sec      # one line per criterion, four-space indent
    assert "  unknowns:" in sec and sample_plan.unknowns[0] in sec
    assert "  verification: " + sample_plan.verification_approach[:80] in sec
    text2 = _inv(..., exclude_plans={sample_plan.id})
    assert sample_plan.id not in _section(text2, "Existing plans and contracts")

def test_plan_index_caps_each_plan(...):
    # a plan whose contracts' statements exceed PLAN_INDEX_CAP renders "[truncated at 2500 characters]" once for that plan
```
Use `_one_line` on every statement and unknown (Task 2 of the previous plan: heading injection).

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_brief.py -q -k plan_index` → FAIL.

- [ ] **Step 3: Implement** per the format in the test: plan line, `  unknowns:` block, `  verification:` line (first 80 chars), then each contract line followed by `    paths: a, b` and one `    A<n>: statement` line per criterion. Cap per plan on the raw rendered text.

- [ ] **Step 4: Run the suite** — `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ateam/brief.py tests/test_brief.py
git commit -m "feat(brief): plan index carries acceptance criteria, unknowns and verification; plans can be excluded"
```

---

### Task 5: Planner outcomes: covered, dependency notes, pending-path records

**Files:**
- Modify: `src/ateam/plans/model.py` (`PlanStatus`, `Plan`)
- Modify: `src/ateam/planner.py` (`report_to_plan`, `PlanResult`, `render_plan_review`)
- Modify: `src/ateam/discovery.py` (`candidates_to_records`)
- Test: `tests/test_planner.py`, `tests/test_discovery.py`

**Interfaces:**
- Produces: `PlanStatus.plan_covered`, `PlanStatus.superseded`; `Plan.covered_by: list[str]`, `Plan.replaces: str | None`; `PlanResult.covered: bool`; `candidates_to_records(..., declared_paths: list[str] = ())` keeping a dead glob when it equals or `matches()` a declared path, with the record's rationale suffixed `" (pending: path declared by this plan)"` and a note in `result.notes` (add `notes: list[str]` to `DiscoveryResult` if absent); `report_to_plan(..., replaces: str | None = None)`.
- Consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

```python
def test_zero_contracts_with_live_dependency_is_covered(...):
    report = {..., "contracts": [], "depends_on_plans": ["P-0001"], ...}
    pr = report_to_plan(report, ..., existing_plans={"P-0001": proposed_plan})
    assert pr.covered and pr.plan.status == PlanStatus.plan_covered and pr.plan.covered_by == ["P-0001"] and pr.contracts == []

@pytest.mark.parametrize("status", [PlanStatus.plan_rejected, PlanStatus.plan_covered])
def test_zero_contracts_naming_a_dead_plan_is_fatal(status, ...):
    with pytest.raises(PlanError, match="no contracts"): ...

def test_zero_contracts_naming_the_replaced_plan_is_fatal(...):
    # replaces="P-0001", depends_on_plans=["P-0001"] -> PlanError "no contracts"

def test_unknowns_mentioning_a_plan_not_in_dependencies_adds_note(...):
    # unknowns=["'verified' comes from P-0002"], depends_on_plans=[] ->
    assert "unknowns mention P-0002, which is not in depends_on_plans" in pr.problems  # notes list used by render

def test_pending_path_record_is_kept_when_a_contract_declares_it(target_repo, ...):
    cand = {..., "paths": ["src/ateam/integrator.py"]}   # file does not exist
    r = candidates_to_records([cand], [], {}, target_repo, records_dir, cap_global=3, declared_paths=["src/ateam/integrator.py"])
    assert len(r.proposed) == 1 and r.proposed[0].rationale.endswith("(pending: path declared by this plan)") and not r.dropped

def test_pending_path_record_is_dropped_without_a_declaring_contract(...):  # today's behaviour, pinned
```
`render_plan_review` for a covered result prints `P-0007 covered by P-0001; no contracts written` and the planner's note.

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_planner.py tests/test_discovery.py -q` → FAIL (AttributeError on new statuses, PlanError on covered case, dropped pending record).

- [ ] **Step 3: Implement**

In `report_to_plan`: compute `live = {pid for pid, p in existing_plans.items() if p.status not in (plan_rejected, plan_covered, superseded, abandoned) and pid != replaces}`; if `not raw`: covered when every id in `depends_on_plans` is in `live` and the list is non-empty, else fatal as today. Covered returns `PlanResult(plan=Plan(status=plan_covered, covered_by=..., unknowns, notes...), contracts=[], proposed=empty DiscoveryResult, covered=True)`. Dependency note: regex `P-\d{4}` over `unknowns + [notes]`, ids in `existing_plans` but not in `depends_on_plans`. Pass `declared_paths=[p for c in contracts for p in c.paths]` to `candidates_to_records`.

- [ ] **Step 4: Run the suite** — `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ateam/plans/model.py src/ateam/planner.py src/ateam/discovery.py tests/test_planner.py tests/test_discovery.py
git commit -m "feat(planner): covered outcome, dependency notes, records for paths a contract declares"
```

---

### Task 6: `ateam plan` covered handling and `--replaces`; approve supersedes

**Files:**
- Modify: `src/ateam/state/db.py` (`FEATURE_TRANSITIONS`)
- Modify: `src/ateam/cli.py` (`plan`, `approve`, `plans list`)
- Test: `tests/test_state_db.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 4 `exclude_plans`, Task 5 statuses and `PlanResult.covered`.
- Produces: `ateam plan "<request>" --replaces P-xxxx`; feature transitions `investigating -> plan_covered`, `plan_proposed -> superseded`.

- [ ] **Step 1: Write the failing tests**

```python
def test_feature_transitions_include_covered_and_superseded(db):
    assert "plan_covered" in FEATURE_TRANSITIONS["investigating"] and "superseded" in FEATURE_TRANSITIONS["plan_proposed"]
    assert FEATURE_TRANSITIONS["plan_covered"] == set() == FEATURE_TRANSITIONS["superseded"]

def test_plan_covered_writes_plan_file_commits_and_transitions(monkeypatch, cli_target):
    # fake report: contracts=[], depends_on_plans=[existing proposed plan]
    # expect exit 0, output contains "covered by", plan file status plan_covered, no contract files added,
    # intent commit message "ateam: plan P-0002 covered by P-0001", feature status plan_covered

def test_plan_replaces_hides_old_plan_from_brief_and_records_it(monkeypatch, cli_target):
    # capture brief_text; assert old id absent from the Existing plans section; new plan file has replaces: P-0001

@pytest.mark.parametrize("status", ["plan_approved", "plan_rejected", "plan_covered"])
def test_plan_replaces_refuses_non_proposed_plan(status, ...):
    # exit 1, message "--replaces needs a plan_proposed plan; P-0001 is <status>", no session run, no feature row created

def test_approve_supersedes_the_replaced_plan(monkeypatch, cli_target):
    # after approve: old plan status superseded with history why "replaced by P-0002", old contracts still draft,
    # feature P-0001 superseded, approval commit includes both plan files

def test_plans_list_shows_covered_and_superseded(...)
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_cli.py tests/test_state_db.py -q -k "covered or replaces or supersede"` → FAIL.

- [ ] **Step 3: Implement**

`plan`: add `@click.option("--replaces", default=None)`; validate before the `requested` transition; pass `exclude_plans={replaces}` and `replaces=replaces`; on `pr.covered`: `save_plan`, `commit_intent(..., f"ateam: plan {pid} covered by {', '.join(pr.plan.covered_by)}", ["plans"])`, `transition(... "plan_covered", by="ateam", reason=...)`, print `render_plan_review`, exit 0. Set `replaces` on the written plan in both outcomes. `approve`: after saving the approved plan and before `commit_intent`, if `plan_.replaces` names a `plan_proposed` plan, save it as `superseded` with a history entry and include it in the commit; after the commit, `transition("feature", old, "superseded", by="owner", reason=f"replaced by {plan_id}")`. The old plan's contracts are untouched.

- [ ] **Step 4: Run the suite** — `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ateam/state/db.py src/ateam/cli.py tests/
git commit -m "feat(cli): plan --replaces, covered outcome, superseded on approval"
```

---

### Task 7: The staff-engineer review pass

**Files:**
- Modify: `src/ateam/roles.py` (`REVIEWER_SPEC`, `REVIEW_SCHEMA`)
- Modify: `src/ateam/brief.py` (`assemble_review_brief`)
- Modify: `src/ateam/plans/model.py` (`PlanReview`, `Plan.review`)
- Modify: `src/ateam/runner/investigate.py` (`resume` parameter)
- Modify: `src/ateam/planner.py` (`render_plan_review` prints the review; `render_draft(result: PlanResult) -> str` YAML of plan + contracts for the review brief)
- Modify: `src/ateam/cli.py` (`plan` pipeline, `--no-review`)
- Test: `tests/test_roles.py` (schema shape), `tests/test_brief.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `REVIEW_SCHEMA = {verdict: "defensible"|"needs_revision", findings: [{contract: str, severity: "blocking"|"important"|"minor", finding: str, suggestion: str}], notes: str}` all required, `additionalProperties: False`.
  - `REVIEWER_SPEC: RoleSpec(role=Role.investigate, tools=["Read","Glob","Grep","Bash"], report_schema=REVIEW_SCHEMA)` whose prompt says: you are a staff engineer reviewing a draft plan for technical defensibility before the owner sees it; for each contract judge whether the acceptance criteria are complete, testable and sufficient for the request, whether the paths are the right files (read them), whether record citations fit, whether the allocation reasoning is justified by the code, whether the checks can run in a fresh worktree, and what failure modes are missing; `blocking` means the owner would be approving something that cannot work or misses the request; never rewrite the plan; `INVESTIGATION_COMMON` appended.
  - `assemble_review_brief(*, target_root, config, records, plans, contracts, scratch_dir, request, draft: str, exclude_plans=frozenset()) -> str` with headings exactly `Rules, Records, Existing plans and contracts, Request, Draft plan, Checks, Rubric, Handoff file` (`draft` demoted two levels).
  - `PlanReview(verdict: Literal["defensible","needs_revision","unavailable"], findings: list[ReviewFinding], revised: bool = False, session_id: str | None = None, cost_usd: float = 0.0)`; `Plan.review: PlanReview | None = None`.
  - `run_investigation(..., resume: str | None = None)` passed to `worker_options`.
  - CLI pipeline: plan session → `report_to_plan` (validate) → unless `--no-review` or covered: review session at the same tier → if `verdict == "needs_revision"` and any `blocking` finding: resume the planner session with prompt `"A staff-engineer review found blocking problems with your plan. Revise the plan to address every blocking and important finding below, keep what was right, and return the complete report again.\n\n<findings as '- [severity] contract: finding. Suggestion: ...'>"`, re-validate with `report_to_plan`, set `review.revised = True` (if the revised report is invalid, keep the first valid plan and add a note) → write plan with `review` → render.

- [ ] **Step 1: Write the failing tests**

```python
def test_review_schema_and_spec_shape(): ...   # required keys, enums, spec role investigate, "staff engineer" in prompt

def test_review_brief_headings(...): assert _headings(text) == ["Rules","Records","Existing plans and contracts","Request","Draft plan","Checks","Rubric","Handoff file"]

def test_plan_runs_review_and_stores_it(monkeypatch, cli_target):
    # fake run_investigation dispatches on spec: PLANNER_SPEC -> plan report; REVIEWER_SPEC -> {"verdict":"defensible","findings":[],"notes":"ok"}
    # assert two sessions ran, plan file has review.verdict == "defensible", output contains "Review: defensible"

def test_plan_blocking_review_triggers_one_revision(monkeypatch, cli_target):
    # reviewer returns one blocking finding; planner is called again with resume == first session id and the finding in the prompt;
    # plan file review.revised is True; exactly three sessions ran; contracts are from the second planner report

def test_plan_important_only_review_does_not_revise(...)       # two sessions, revised False, findings stored

@pytest.mark.parametrize("failure", ["tamper", "crash", "no_report"])
def test_plan_survives_review_failure(failure, ...):
    # plan written, review.verdict == "unavailable", exit 0, warning "review unavailable" printed

def test_plan_no_review_flag_skips_review(...)                  # one session, review is None
def test_covered_plan_is_not_reviewed(...)                      # one session
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_roles.py tests/test_brief.py tests/test_cli.py -q -k review` → FAIL.

- [ ] **Step 3: Implement** per the interfaces. In `cli.plan`, wrap the review session in `try/except Exception` and treat tamper or `report is None` the same way: `PlanReview(verdict="unavailable")` and `click.echo("warning: review unavailable (<reason>); the plan is unreviewed")`. Telemetry: the review and revision sessions start their own attempts via `run_investigation` with `name=f"{pid}-review"` and `name=pid` respectively. Print `session` lines for every session and a total cost line.

- [ ] **Step 4: Run the suite** — `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ateam/roles.py src/ateam/brief.py src/ateam/plans/model.py src/ateam/runner/investigate.py src/ateam/planner.py src/ateam/cli.py tests/
git commit -m "feat(plan): staff-engineer review of the draft plan with one revision round"
```

---

### Task 8: Setup runs once per worktree

**Files:**
- Modify: `src/ateam/runner/run.py` (`ensure_setup`)
- Modify: `src/ateam/cli.py` (`worker_run`, before `run_contract_attempt`)
- Test: `tests/test_run.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `ensure_setup(worktree: Path, config: TargetConfig, db: StateDB) -> list[str]` returning the commands it ran (empty when already set up or `config.setup` is empty); state meta key `setup:<resolved worktree path>` holding an ISO timestamp; `SetupError(cmd, returncode, output)` on the first failing command, with nothing recorded.

- [ ] **Step 1: Write the failing tests**

```python
def test_ensure_setup_runs_commands_once_per_worktree(target_repo, ateam_home, tmp_path):
    cfg = TargetConfig(setup=["touch setup-ran", "echo second"])
    ran = ensure_setup(target_repo, cfg, db)
    assert ran == cfg.setup and (target_repo / "setup-ran").exists()
    (target_repo / "setup-ran").unlink()
    assert ensure_setup(target_repo, cfg, db) == [] and not (target_repo / "setup-ran").exists()

def test_ensure_setup_failure_records_nothing(...):
    cfg = TargetConfig(setup=["false"])
    with pytest.raises(SetupError) as e: ensure_setup(...)
    assert db.get_meta(f"setup:{target_repo.resolve()}") is None

def test_worker_run_runs_setup_before_the_attempt_and_stops_on_failure(monkeypatch, cli_target_with_worktree):
    # config setup ["false"]: exit 1, output contains "setup failed: false", no attempt rows added
```
Commands run with `subprocess.run(cmd, shell=True, cwd=worktree, env={**os.environ, **config.env}, capture_output=True, text=True)`.

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_run.py tests/test_cli.py -q -k setup` → FAIL (ImportError).

- [ ] **Step 3: Implement**; in `worker_run` call `ensure_setup` after the worktree checks and before `run_contract_attempt`, printing `setup: <cmd>` per command run, and `_fail(f"setup failed: {e.cmd} (exit {e.returncode})\n{e.output}")` on `SetupError`.

- [ ] **Step 4: Run the suite** — `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ateam/runner/run.py src/ateam/cli.py tests/test_run.py tests/test_cli.py
git commit -m "feat(worker): run config.setup once per worktree before the first attempt"
```

---

### Task 9: TEST `requirements_unclear` and attempt classification

**Files:**
- Modify: `src/ateam/roles.py` (`TEST_SCHEMA`, TEST prompt)
- Modify: `src/ateam/runner/run.py` (`run_contract_attempt` outcome for `Role.test`; `classify_test_report(report: dict) -> tuple[str, str | None]`)
- Modify: `src/ateam/cli.py` (`worker_run` prints the classification)
- Test: `tests/test_roles.py`, `tests/test_run.py`

**Interfaces:**
- Produces: `TEST_SCHEMA.properties.requirements_unclear: {"type": "boolean"}`, required; `classify_test_report(report) -> ("completed", None)` when no `not_met`, `("failed", "requirements")` when any `not_met` and `requirements_unclear` is true, `("failed", "implementation")` when any `not_met` otherwise. `cannot_determine` alone is `completed` (the pause path is pass two).

- [ ] **Step 1: Write the failing tests**

```python
def test_test_schema_requires_requirements_unclear(): assert "requirements_unclear" in TEST_SCHEMA["required"]
def test_test_prompt_explains_the_flag(): assert "requirements_unclear" in ROLE_SPECS[Role.test].system_prompt

@pytest.mark.parametrize("verdicts,flag,expected", [
    ([("A1","met")], False, ("completed", None)),
    ([("A1","not_met")], False, ("failed", "implementation")),
    ([("A1","not_met")], True, ("failed", "requirements")),
    ([("A1","cannot_determine")], True, ("completed", None)),
])
def test_classify_test_report(verdicts, flag, expected): ...

@pytest.mark.asyncio
async def test_test_attempt_records_classification(monkeypatch, ...):  # like test_tier_override_is_recorded...: role test, fake report with not_met + flag -> telemetry outcome failed, failure_class requirements
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_roles.py tests/test_run.py -q -k "requirements or classify"` → FAIL.

- [ ] **Step 3: Implement**. Prompt sentence: "Set requirements_unclear to true when a criterion cannot be judged fairly because the contract itself is ambiguous or contradictory; say which criterion and why in notes. Leave it false when the code is simply wrong." In `run_contract_attempt`, when `role is Role.test and res.report and scope.ok`, use `classify_test_report` for `outcome, failure_class`. `worker_run` prints `classification: <outcome>/<failure_class or ->` for TEST.

- [ ] **Step 4: Run the suite** — `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ateam/roles.py src/ateam/runner/run.py src/ateam/cli.py tests/
git commit -m "feat(test): requirements_unclear flag and attempt classification"
```

---

### Task 10: Documentation and examples

**Files:**
- Modify: `README.md` (command list: `plan --replaces/--no-review`, default tier, setup per worktree, review pass, covered outcome)
- Modify: `docs/first-run-walkthrough.md` (the `plan` step shows the review block and the cost of three sessions; a note that `--tier standard` halves it)
- Modify: `examples/config.example.yaml` (`setup:` example `[".venv/bin/pip install -e '.[dev]'"]` with a comment that it runs once per worktree)
- Modify: `examples/plan.example.yaml` (`review:` block, `replaces: null`, `covered_by: []`)
- Test: `tests/test_examples.py` (or wherever examples are validated; `grep -rn plan.example tests/`)

- [ ] **Step 1: Run the examples test to see what it validates** — `.venv/bin/pytest -q -k example`.
- [ ] **Step 2: Update the four files**; keep sentences short; no "now".
- [ ] **Step 3: Run the suite** — `.venv/bin/pytest -q` → PASS.
- [ ] **Step 4: Commit**

```bash
git add README.md docs/first-run-walkthrough.md examples/
git commit -m "docs: plan review, --replaces, covered outcome, setup per worktree"
```
