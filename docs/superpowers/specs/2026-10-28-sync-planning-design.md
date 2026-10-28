# Sync planning, interface checks, fan-out and targeted tests: design

Date: 2026-10-28. Status: approved by the owner in chat ("Do it", 2026-10-28). Amends the planner and plan approval (`docs/superpowers/specs/2026-10-24-ateam-harness-design.md`), verify, the BUILD and TEST roles, and lanes (`2026-10-27-lanes-design.md`, decision 1).

## Intent

The owner wants several areas of work planned at once with shared context, so that contracts touching the same ground agree their shared shapes during planning: the signatures, file formats and CLI flags they produce for each other. Each BUILD then works against a settled shape. There are no surprises at merge, and the rework of a failed verify, a new BUILD attempt or a rebase BUILD over a mismatched shape does not happen.

The first self-dogfood (P-0018, `docs/dogfood/2026-10-27-first-self-runs.md`) also showed where a contract's time goes. About 85% of BUILD and TEST session time was spent waiting on test runs. Model thinking was 0.4 to 8 minutes per session. Each contract ran the full suite (1,643 tests) about five times: in BUILD, two or three times in TEST, and once in verify. Two sessions and a verify running `pytest -n auto` at once also slowed each other from about 3 minutes to 5 to 10 minutes per run. A contract took about 36 minutes; the target is 10 to 15.

This design is the first of two steps. This plan builds the four changes below with today's planner. The second plan covers the six unbuilt areas as one synced plan, and is the trial: event logs, the watch and tmux viewer, doctor repair modes, calibration, `abandon` and worktree cleanup.

## Goals

- Shared shapes are agreed once, at planning, with one owning contract each, and the owner approves them as part of the plan.
- A plan whose contracts disagree on a shared shape cannot be approved.
- Verify fails a contract that did not build a shape it promised.
- Contracts that depend on one shape-owning contract run in parallel once it is integrated, unless their declared files overlap.
- BUILD and TEST sessions run the contract's own tests, not the full suite. Verify keeps running the full checks once per contract, so "verified" means what it means today.

## Non-goals

- Changing what verify runs (finding T1) or letting merges overlap running sessions (T2). They stay open findings.
- Claude triage of planning effort (C1).
- A new store table, record kind or config key.
- Enforcing targeted runs with the policy. A session may still run the full suite when it has a reason.

## Anti-goals

- **A parallel subsystem.** Everything extends the modules that own the concern (CLAUDE.md, "Integrate, don't duplicate"): `ateam.planner`, `ateam.roles`, `ateam.brief`, `ateam.verify`, `ateam.scheduler`, `ateam.cli`, and the `Contract` and `Plan` models.
- **Forced categories.** No one assigns a contract a "foundation" type. Owners and consumers fall out of the interfaces the planner already declares.
- **A weaker verify.** The full configured checks still run for every contract.

## Decided by the owner (2026-10-28)

1. **Sync at planning.** The planner first fixes the plan's shared shapes, each with one owning contract, then writes contracts that produce or consume those exact shapes. The plan shows the shapes at approval as the agreement the owner signs off.
2. **A mismatch blocks approval.** It is no longer only a note.
3. **Verify checks produced interfaces.**
4. **A dependency alone no longer joins lanes.** Only overlapping declared files put contracts in one lane. A contract still starts only when its in-plan dependencies are integrated, so it is cut from an integration tip that already holds them.
5. **Targeted test runs in BUILD and TEST,** folded into this trial.
6. **Built by ATeam as a normal plan in today's format,** then the six-area plan is the synced trial, measured against P-0018.
7. **A lane's later contract fast-forwards to the integration tip.** Before a later contract in a lane starts, the lane's branch is fast-forwarded to the current integration tip, and that tip is recorded as the contract's base. This amends lanes decision 2 (`2026-10-27-lanes-design.md`), "each contract's recorded base is the previous contract's verified head".
   - The fast-forward is always clean: the previous contract in the lane is already merged, so the tip contains the lane branch.
   - The tip also holds any dependency from another lane, because a contract starts only when its dependencies are integrated.
   - A later contract starts exactly where a contract in its own worktree would. The lane keeps its one worktree and setup, and scope, tamper and TEST still judge only the contract's own diff.

## Calls made by the assistant (owner to check)

- **No new model for shapes.** A shared shape is an interface that one contract of the plan produces and another consumes. The approval view derives the shapes table from the contracts' existing `interfaces`.
- **Approval rules.** `ateam approve` refuses a plan in which:
  - a consumer's signature differs from the in-plan producer's signature (whitespace-normalised, as today);
  - two contracts produce the same name;
  - a contract consumes a shape another contract of the plan produces without depending on that contract.

  The producer may come anywhere in the list, not only earlier, so the planner is not forced into an order. The plan review session reports each case as a blocking finding, so the revision session fixes it before the owner sees the plan.
- **Signature form.** The planner prompt asks for each code interface's signature as its exact header line: `def name(...) -> T:`, `async def ...`, or `class Name(Base):`, with a method's class named in `name` (`Class.method`). The approval view flags a code-like interface that is not in this form, as a note.
- **Verify's interface check** covers only `produces` interfaces whose signature is in that header form. The header must be defined in a file inside the contract's paths, incidental paths included, and match the signature. The comparison is of the header alone, from `def`/`class` to the closing colon, with whitespace normalised; decorators and the body are ignored. Every other interface is reported as "not checked by minimal verify", never failed: CLI flags, file formats, constants, attributes, and free-form signatures, including those in contracts frozen before this change. A failed check fails verify with the same failure class as a missing acceptance test.
- **Plans proposed before this change** are judged by the new approval rules: `ateam approve` refuses one that breaks them and tells the owner to re-plan. None is waiting today.
- **The shapes table is shown, not confirmed.** It appears in the plan view, in `ateam plans show` and in `ateam approve`'s output. Approving the plan is the sign-off; the approval gate gets no new prompt.
- **Targeted runs come from the brief, not a new config key.** The brief lists the contract's tests: the files and `path::name` references of its `test` acceptance criteria, plus the test files in its declared paths. It labels the configured checks "verify runs these after you finish". The BUILD and TEST role prompts change to match: run the contract's tests while working, and leave the full checks to verify. Neither role is told it may never run the full suite.
- **Lane order with outside dependencies.** A lane whose next contract waits on a dependency in another lane must not block contracts later in the same lane that are ready. Lanes are still ordered dependencies first, then by contract id, as today. The builder decides how a lane skips a contract that is waiting.
- **The trial's measure** is recorded in the dogfood findings doc: minutes per contract, full-suite runs per session (from transcripts), failed verifies or rebase BUILDs caused by a shape mismatch, total wall clock and cost.

## Verification the builder must plan

- Table-driven planner tests for the approval rules: a matching consumer; a signature mismatch; two producers; a consumer without the dependency; a producer later in the list than its consumer (allowed); a consumer of existing code (unchanged behaviour).
- An approval test showing the shapes table, and `ateam approve` refusing a plan with a mismatch.
- Verify tests: a produced function present with the signature; present with a different signature (fail); missing (fail); a non-code interface (reported, not failed).
- Worktree tests: a lane's later contract has the integration tip as its base, and its worktree holds a dependency integrated from another lane.
- Lane tests: three contracts depending on one, with disjoint paths, form four lanes, and the three run in parallel after the first is integrated. Overlapping files still join lanes. Update the existing test that expects dependencies to join lanes, and record why.
- Brief tests: the brief lists the contract's tests and labels the configured checks as verify's. Role prompt tests, if the suite has them, are updated.
- The P-0018 shape still dry-runs to sensible lanes.

## Left to the builder

- The wording of the brief's test section, the role prompts, and the approval table.
- How the scheduler holds a waiting contract inside a lane.
- How the signature comparison finds the definition line (reuse `verify._defines` where it fits).
