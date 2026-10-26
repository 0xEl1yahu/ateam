# Dogfood planning fixes Implementation Plan

> Use superpowers:executing-plans. Implement task-by-task with regression tests first and a fresh final review.

Goal: remove misleading interface diagnostics and retain review failure evidence.
Architecture: advisory planner validation and existing PlanReview.notes; no schema, permission, approval or snapshot changes.
Spec: existing harness design and the owner conversation of 2026-10-26. Important-only revision and handoff changes await owner approval and are excluded from this implementation.

Constraints: isolated worktree from main 1beed29; exact signatures remain binding; no live model sessions; no writes to existing intent.
Review focus: qualified methods on the wrong class; grouped names versus paths/CLI interfaces; bounded searches and symlink confinement; saved failure reasons; concurrent ref changes still discard reports.

### Task 1: Interface diagnostics
Files: src/ateam/planner.py, src/ateam/roles.py, tests/test_planner.py.
- [x] Add regressions for qualified Python methods/functions, grouped declarations, incomplete lookup and non-Python interfaces.
- [x] Watch the new regressions fail.
- [x] Supplement bounded text lookup with per-file Python AST definitions; confine reads to repository. Diagnose clear symbol groups, preserve paths and CLI names. Report unresolved lookup without claiming code does not exist. Keep whitespace-normalized exact signature matching.
- [x] Prompt for one interface per entry, exact producer signatures.
- [x] Run .venv/bin/pytest -q tests/test_planner.py tests/test_roles.py, then full suite; commit.

### Task 2: Durable review diagnostics
Files: src/ateam/cli.py, tests/test_cli.py, tests/test_investigate.py, docs/first-run-walkthrough.md.
- [x] Test persisted and displayed crash, tamper, missing-report and malformed-report reasons; real ref change still invalidates review.
- [x] Watch diagnostic regressions fail (the ref guard is existing behavior).
- [x] Populate PlanReview.notes with the displayed failure reason, retain available result details, use neutral repository-change wording.
- [x] Document concurrent branch creation invalidation and recovery by rerunning planning once the checkout is stable.
- [x] Run .venv/bin/pytest -q tests/test_cli.py tests/test_investigate.py tests/test_snapshot.py, then full suite; commit.

Finish: fresh whole-branch review, address important findings with RED/GREEN tests. Leave branch ready for owner review; do not merge while the two behavior decisions remain pending.

Verification: baseline 1186 passed, 2 skipped, 3 xfailed; focused 218 passed; final full suite 1204 passed, 2 skipped, 3 xfailed. Fresh independent review found no Critical or Important defects.

Deferred minors: grouped producer entries are not separately diagnosed; valid verdict plus invalid findings has no dedicated persisted-reason test. Grouped consumers and malformed non-review reports are covered.

Owner decisions pending: important-only revision and investigation handoff behavior. No gate or worker permissions changed.
