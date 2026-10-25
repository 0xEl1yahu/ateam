# Planner Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give discovery and planning sessions what they were missing on the first dogfood run: the target's rules, the proposed records, an index of existing plans and contracts, a prompt written for an investigation rather than a worker, a schema that asks for files read, scope boundaries, verification approach and cross-plan dependencies, and budgets derived from the tier when the planner leaves them blank.

**Architecture:** One new assembler, `assemble_investigation_brief`, replaces the two ad-hoc strings in `cli.py`. It renders labelled sections: Rules (the target's `CLAUDE.md` and `AGENTS.md`, plus documents named in config), Records (approved, then proposed marked unapproved), Existing plans and contracts (id, status, title, paths), the Request, Checks, Tier names, the Rubric, and the Handoff file line. `roles.py` splits the shared role text into a worker version and an investigation version; the planner schema gains four fields; the `Plan` model stores them; `report_to_plan` validates `depends_on_plans` and derives budgets from the tier map; the review prints the new fields.

**Tech Stack:** unchanged.

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md` (Discovery, Allocation, Roles) and the 2026-10-25 owner decisions. This plan implements fixes 1 to 5 and 9 from the dogfood review; fixes 6, 7, 8, 11 and 12 are owner decisions and are out of scope here.

## Global Constraints

- Everything in `CLAUDE.md`: worktree, test first, nothing written into a target, intent-repo commits with the `ateam:` prefix, no `rm` for workers, enforcement untouched by this plan.
- Investigation briefs are plain text with `## ` headings in this order: `Rules`, `Records`, `Existing plans and contracts`, `Request`, `Checks`, `Tiers`, `Rubric`, `Handoff file`. Discovery omits `Request`.
- Per-document cap in the Rules section: 12,000 characters per file, with a `[truncated at 12000 characters]` marker. Proposed records in the Records section are headed `Proposed, not yet approved (not binding; do not re-propose):`.
- Policy, allow-lists, tiers data file and the snapshot are not touched.

## Review Focus

- A target with no `CLAUDE.md`, no `AGENTS.md`, no plans and no records still gets a brief with every heading and plain "none" lines, never an exception. (Task 2)
- A `depends_on_plans` entry naming a plan that does not exist is reported as a note, never fatal, and the plan still saves. (Task 3)
- A contract whose `budget` is omitted gets `usd_per_attempt` and `max_turns` from its BUILD tier and `attempts` 2 for light, 3 otherwise; a budget the planner did give is kept untouched. (Task 3)
- The investigation role prompt contains no reference to a frozen contract or to a "Task" section, and the worker prompt is unchanged byte for byte except for the extracted shared text. (Task 1)
- The CLAUDE.md of the target is included even when the target's config has no `brief_docs`. (Task 2)

---

## File structure

```
src/ateam/roles.py             split COMMON -> WORKER_COMMON, INVESTIGATION_COMMON; PLANNER_SCHEMA + 4 fields; INVESTIGATION_PROMPT_TEMPLATE text
src/ateam/config.py            TargetConfig.brief_docs: list[str] = []
src/ateam/brief.py             assemble_investigation_brief(...) -> str
src/ateam/plans/model.py       Plan + depends_on_plans, files_read, out_of_scope, verification_approach
src/ateam/planner.py           report_to_plan stores the fields, notes unknown depends_on_plans, derives budgets; render_plan_review prints them
src/ateam/allocation.py        budget_for(tier_name, tiers) -> Budget
src/ateam/cli.py               init and plan use assemble_investigation_brief and a fuller user turn
examples/config.example.yaml   brief_docs
tests: test_roles.py, test_brief.py, test_plan_model.py, test_planner.py, test_allocation.py, test_cli.py (additions)
```

---

### Task 1: Split the role text; extend the planner schema

**Files:** Modify `src/ateam/roles.py`; Test `tests/test_roles.py`, `tests/test_investigate.py`

**Interfaces:**
- `roles.WORKER_COMMON: str` is the current `COMMON` verbatim (BUILD, INVESTIGATE-as-worker, TEST specs keep using it; `COMMON` stays as an alias for it so nothing else breaks).
- `roles.INVESTIGATION_COMMON: str`: "You are one investigation session inside ATeam, an engineering harness. You read and reason; you never modify files or state, and shell commands are limited to read-only inspection. Your brief below has labelled sections: Rules (the target's own instructions; authoritative), Records (approved records are binding; proposed ones are not and must not be re-proposed), Existing plans and contracts (what is already planned; avoid overlap and name dependencies), Request, Checks, Tiers, Rubric, and Handoff file. " + `AUTHORITY` + " Keep the handoff file named under 'Handoff file:' up to date; it lives outside the repository. It is a YAML mapping with the keys progress, unresolved, files, hypotheses and next. End by producing the structured report you were asked for."
- `DISCOVERY_SPEC` and `PLANNER_SPEC` use `INVESTIGATION_COMMON`. `PLANNER_SPEC`'s own text adds: "Read the Rules section first and treat anything it marks as the owner's decision as out of your scope: raise it under unknowns instead of deciding it. Cite in files_read every repository file you opened. State in out_of_scope what you deliberately left out. Describe in verification_approach how the finished feature would be checked end to end. Name in depends_on_plans any existing plan this one needs merged first."
- `PLANNER_SCHEMA` gains top-level required fields: `depends_on_plans: [str]`, `files_read: [str]`, `out_of_scope: [str]`, `verification_approach: str`.

- [ ] Write failing tests: `test_investigation_common_has_no_worker_promises` (no "frozen contract", no "Task (", no "Binding (" in `INVESTIGATION_COMMON`; "Handoff file" present), `test_worker_specs_unchanged` (`ROLE_SPECS[Role.build].system_prompt` contains `WORKER_COMMON` and `COMMON is WORKER_COMMON`), `test_planner_schema_new_fields_required` (the four names in `PLANNER_SCHEMA["required"]`), update `test_discovery_and_planner_specs_are_investigate_and_strict` for the new field set.
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: investigation role text; planner schema asks for files read, scope, verification, plan dependencies`.

### Task 2: The investigation brief assembler

**Files:** Modify `src/ateam/config.py`, `src/ateam/brief.py`, `examples/config.example.yaml`; Test `tests/test_config.py`, `tests/test_brief.py`, `tests/test_examples.py`

**Interfaces:**
- `config.TargetConfig.brief_docs: list[str] = []` (paths relative to the target root, included in investigation briefs).
- `brief.assemble_investigation_brief(*, kind: Literal["discovery", "planning"], target_root: Path, config: TargetConfig, records: dict[str, Record], plans: dict[str, Plan], contracts: dict[str, Contract], scratch_dir: Path, request: str | None = None, max_candidates: int | None = None) -> str`:
  - `## Rules`: for each of `CLAUDE.md`, `AGENTS.md` (if present at the root) and each `config.brief_docs` entry that exists: a sub-heading `### <path>` and the file text capped at 12,000 characters with the marker. If none exist: `No rules files found (CLAUDE.md, AGENTS.md, config brief_docs).` Missing `brief_docs` entries are listed as `missing: <path>`.
  - `## Records`: approved records as `- R-0001 invariant [paths or global] statement`; then `Proposed, not yet approved (not binding; do not re-propose):` with the same line format for `proposed` records; `none` lines when empty.
  - `## Existing plans and contracts`: one line per plan `P-0001 plan_proposed  <title>` followed by its contracts indented `  C-0001 draft  <title>  [paths]`; `none` when empty.
  - `## Request` (planning only): the request text. For discovery instead: `## Task` is NOT used; discovery gets `## Discovery limits` with `Propose at most N candidates; at most <global_record_cap> may be global.`
  - `## Checks`: configured checks as `name: cmd`, or `none configured`.
  - `## Tiers`: `light, standard, careful; investigate may also be none.`
  - `## Rubric`: `allocation.rubric_text()`.
  - `## Handoff file`: `<scratch_dir>/handoff.yaml`.
- `examples/config.example.yaml` gains `brief_docs: ["docs/design.md"]` with a comment.

- [ ] Write failing tests: `test_brief_docs_default_empty_and_roundtrip`, `test_investigation_brief_headings_in_order_and_empty_target` (bare repo: every heading present, "No rules files found", "none" lines, no exception), `test_rules_section_includes_claude_md_and_caps_long_docs` (a 20,000-char doc gets the marker; CLAUDE.md included without config), `test_records_section_separates_proposed`, `test_plans_section_lists_contracts_with_paths`, `test_discovery_kind_has_limits_not_request`, `test_handoff_line_present`, `test_config_example_loads` (unchanged test still passes with the new key).
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: investigation brief with rules, proposed records and plan index`.

### Task 3: Plan fields, budgets from tier, review output

**Files:** Modify `src/ateam/plans/model.py`, `src/ateam/allocation.py`, `src/ateam/planner.py`, `examples/plan.example.yaml`; Test `tests/test_plan_model.py`, `tests/test_allocation.py`, `tests/test_planner.py`, `tests/test_examples.py`

**Interfaces:**
- `Plan` gains `depends_on_plans: list[str] = []`, `files_read: list[str] = []`, `out_of_scope: list[str] = []`, `verification_approach: str = ""`; all excluded from nothing (they are part of `content_hash`).
- `allocation.budget_for(tier_name: str, tiers: dict[str, dict[Role, TierSpec]]) -> Budget`: `attempts = 2 if tier_name == "light" else 3`, `usd_per_attempt = tiers[tier_name][Role.build].usd`, `max_turns = tiers[tier_name][Role.build].max_turns`.
- `planner.report_to_plan(..., tiers: dict | None = None, existing_plans: dict[str, Plan] | None = None)`: when a contract has no `budget`, use `budget_for(final build tier, tiers or load_tiers())`; when the planner gave one, keep it. `depends_on_plans` entries not in `existing_plans` produce a note `depends_on_plans names unknown plan P-00XX`; entries equal to the plan's own id are dropped with a note. The four fields are copied onto the `Plan`.
- `planner.render_plan_review` prints, after the contracts: `Depends on plans:`, `Files read:` (count and the first 20), `Out of scope:`, `Verification approach:`.
- `examples/plan.example.yaml` gains the four fields.

- [ ] Write failing tests: `test_plan_new_fields_default_and_hash`, `test_budget_for_each_tier`, `test_missing_budget_derived_from_build_tier_and_given_budget_kept`, `test_depends_on_plans_unknown_is_note_and_self_dropped`, `test_review_prints_new_sections`, `test_plan_example_loads` (unchanged test passes).
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: plan fields for dependencies, files read, scope and verification; budgets from tier`.

### Task 4: Wire the CLI and the user turn

**Files:** Modify `src/ateam/cli.py`, `README.md`; Test `tests/test_cli.py`

**Interfaces:**
- `init` and `plan` build their brief with `assemble_investigation_brief` (loading plans and contracts from the intent repo), pass `scratch_dir = paths.scratch_root / name`, and pass `existing_plans` and `tiers` into `report_to_plan`.
- User turn for discovery: "Discover this repository's existing engineering intent. Read the Rules section of your brief first, then the code and history. Report candidates with evidence, and list unknowns." For planning: "Produce the plan for the Request in your brief. Read the Rules section first; anything it reserves for the owner goes under unknowns, not into a contract. Fill every field of the report: contracts, proposed_records, depends_on_plans, files_read, out_of_scope, verification_approach, unknowns, notes."
- `_records_index` is removed (superseded).
- README: one sentence under "Discover" and "Plan" saying the brief now carries the target's CLAUDE.md, proposed records and the plan index, and that `brief_docs` in config adds documents.

- [ ] Write failing tests (fake `run_investigation` capturing `brief_text` and `prompt`): `test_plan_brief_contains_rules_records_and_plan_index` (a CLAUDE.md in the target, one proposed record, one existing plan with a contract: all appear under the right headings; the user turn names `files_read`), `test_init_brief_has_discovery_limits_and_rules`, `test_plan_stores_new_fields_and_review_prints_them` (canned report with the four fields; `plans show` includes `verification_approach`), `test_plan_missing_budget_uses_tier`.
- [ ] Run → FAIL. Implement. Run whole suite → PASS. Commit: `feat: init and plan use the investigation brief and a fuller user turn`.
