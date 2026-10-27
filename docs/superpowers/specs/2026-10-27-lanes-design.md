# Lanes and conflict recovery: design

Date: 2026-10-27. Status: approved by the owner (2026-10-27; approval relayed by session ateam-86), as amended by "Owner rulings at planning" below. Amends the scheduler (`docs/superpowers/specs/2026-10-24-ateam-harness-design.md`, "`ateam run` (the scheduler)" and "Verification and integration") and the pause and resume design (`2026-10-26-pause-resume-design.md`).

## Intent

A plan's contracts run in separate git worktrees, two at a time. Independent contracts are cut from the same integration tip and merged one by one as each is verified. When two of them edit the same lines, the second merge conflicts. Today that pauses the plan, and `ateam resume` refuses it because the rebase BUILD was never built. The contract's verified work is stranded: wasted sessions, wasted money and a dead plan.

The owner wants related work to be reconciled in order and integrated without waste, without forcing contracts into invented categories. The answer is to sort contracts that touch the same areas into lanes. A lane runs its contracts one after another in one shared worktree. Unrelated contracts run in parallel as they do today, and a conflict that slips through anyway is recovered instead of stranding the plan.

## Goals

- Contracts whose declared paths overlap, or that depend on each other, never run on the same base at the same time.
- Related contracts share one worktree and one environment setup, each starting from the previous contract's verified work.
- Unrelated contracts keep full parallelism.
- A merge conflict that happens anyway is recovered by one rebase BUILD, not by a dead end.
- Every per-contract guarantee holds: frozen contract, scope check, tamper check, independent TEST, verify, merge at verify.

## Non-goals

- Merging the integration tip into a contract before verify ("verify against the tip"). It makes verify's scope base a moving target for little gain once lanes exist.
- Speeding up verify, which runs the full suite per contract. That is separate work.
- Lanes across plans: one running plan per target stays the rule.
- A cleanup command for worktrees (still deferred).

## Anti-goals

- **Forced categories.** Nobody assigns a lane type. Lanes fall out of what the plan already declares.
- **Shared worktrees in parallel.** Two sessions never edit one worktree at the same time.
- **Weaker checks inside a lane.** A lane never blurs which contract made which change.
- **A parallel subsystem.** Lanes live in the existing scheduler and worktree modules (record R-0025).

## Decided by the owner (2026-10-27)

1. **Lanes are derived, not assigned.** Two contracts of a plan are in the same lane when their declared paths overlap or one depends on the other, and a lane is the connected group. A contract that overlaps nothing is a lane of one: the one-off job, with its own worktree as today.
2. **Within a lane, contracts run one after another in one shared worktree.** Each contract's recorded base is the previous contract's verified head, so its scope, tamper and TEST checks judge its own diff only.
3. **Lanes run in parallel,** up to `max_workers`.
4. **Prevent, then recover.** Lanes prevent the predictable conflicts. The spec's rebase BUILD recovers the unpredictable ones, such as two lanes touching a shared import neither declared.
5. **Integrate, don't duplicate.** Lanes and the rebase BUILD extend `ateam.scheduler`, `ateam.worktrees` and `ateam.retry`; no new subsystem.

## Calls made by the assistant (owner to check)

- **Overlap test.** Two contracts overlap when any path in the target matches a glob of each, or when either contract's glob, matched literally, names a path the other declares (this covers new files that do not exist yet). Incidental paths count. It is conservative: a false overlap costs parallelism, a missed one costs a conflict that the rebase BUILD then recovers.
- **Lane order.** Dependencies first, then contract id.
- **A lane's worktree** is `<home>/worktrees/<first contract id>`, on branch `ateam/<first contract id>`, cut from the integration tip when the lane's first contract becomes ready. Later contracts in the lane continue on the same branch. The worktree's setup runs once per lane.
- **Merge at verify is unchanged.** Each contract in a lane merges onto `ateam/<plan>` as soon as it is verified. The next contract in the lane starts from that verified head, which the integration branch already contains, so it never conflicts with its own lane.
- **A failure inside a lane** blocks the lane's later contracts. They build on it, so they wait, and a pause names the blocked contracts. Other lanes continue until the pause halts the run, as today.
- **Rebase BUILD.** On a merge conflict:
  1. The scheduler cuts a fresh worktree for the conflicting contract from the current integration tip.
  2. It merges the contract's verified head into that worktree, leaving the conflict markers.
  3. One BUILD attempt gets a brief whose only task is to resolve the conflict without changing the contract's behaviour.
  4. The contract is verified again with the new base.

  The attempt counts toward `budget.attempts`, with failure class `environment`. If it fails or conflicts again, the plan pauses `environment_failure` as today. `ateam resume` then accepts a conflict pause, with the rebase BUILD as its next step instead of a refusal.
- **Dry run** prints the lanes: each lane's contracts in order, with which lanes run in parallel.

## Owner rulings at planning (2026-10-27)

These replace the matching assistant calls above where they differ.

6. **The rebase BUILD reopens a verified contract through one new transition, `verified -> building`,** used only by the rebase BUILD. The contract file and the state store move together. Verify then walks `building -> verifying -> verified` as for any BUILD. The conflict pause itself leaves the contract `verified`.
7. **The rebase BUILD runs in the contract's own worktree** (its lane's), not a fresh one. The scheduler merges the integration tip into it without committing and leaves the conflict markers (workers may not run `git merge`). The BUILD resolves them and concludes the merge with `git commit`. `base:<cid>` becomes that integration tip. The lane's later contracts build on the rebased head with no second branch and no second setup.
8. **After a rebase BUILD, TEST runs, then verify.** It is one attempt: any failure at BUILD, TEST or verify pauses `environment_failure` with no retry.
9. **The rebase BUILD counts toward `budget.attempts`, and the limit holds.** A conflict on a contract with no attempt left pauses `environment_failure`, marked as a conflict, with a detail saying so. `ateam resume` opens a fresh window as it does for any pause, and the rebase BUILD runs then.

10. **Setup re-runs on each new base.** The setup marker covers the worktree and the base, so a lane follower, or a contract after its rebase merge, runs `config.setup` again. A contract's environment always reflects its base, as it does today. This replaces "the worktree's setup runs once per lane".
11. **Leftovers in a lane worktree pause; ATeam deletes nothing.** Before a follower's first BUILD, a lane worktree with untracked or modified files pauses `environment_failure` and names them.
12. **A conflict in a path the contract may not edit pauses at once.** If any conflicting path is outside the contract's `paths` (an incidental file such as a lock file), no rebase BUILD runs. The plan pauses `environment_failure` as a conflict and names those paths.
13. **A target-branch conflict stays refused by resume.** A conflict from `ateam integrate` merging a moved target branch names no contract, so there is nothing to rebuild. `ateam resume` refuses it and the way out is to reject the plan. A recovery for it is separate work.

Mechanics decided by the builder and stated at planning: lanes are computed from the files at the plan's recorded integration start (the target tip for a dry run or at approval). A contract's lane worktree is recorded in the state store when it is first cut, so a later run never moves it.

## How it works

Planning is unchanged. Claude, as the planner, still declares each contract's paths and dependencies. The owner also sees the lanes at approval, because `ateam approve` and `--dry-run` print them. A planner that wants more parallelism declares narrower paths, and record R-0024 already fences contracts that share a file to named functions.

The scheduler groups the plan's contracts into lanes before the main loop starts, and starts a lane instead of a contract. A lane's pipeline runs its contracts in order: cut or reuse the worktree, then BUILD, TEST, verify and merge at verify for each, then move to the next with the base set to the verified head. The gate, the budget, attempt windows, pauses, the owner stop and investigations are unchanged; they see contracts exactly as today.

The plan's spec deviation "a conflict when merging a verified contract pauses the plan and leaves the contract verified, instead of the one rebase BUILD of step 3" is withdrawn, as is resume's refusal of conflict pauses.

## Verification the builder must plan

- Unit tests for the lane grouping, driven by a table of path and dependency sets: overlapping globs, a glob against a literal new file, dependencies, incidental paths, and disjoint contracts.
- Scheduler tests with fake sessions:
  - two overlapping contracts run one after another in one worktree, and the second's base is the first's verified head;
  - disjoint contracts still overlap in time;
  - a failure mid-lane blocks the rest of that lane only;
  - setup runs once per lane.
- A rebase BUILD test: a forced conflict between two lanes leads to one rebase BUILD, a re-verify and integration, and the plan reaches `merge_proposed`. A second conflict pauses, and resume runs the rebase BUILD.
- Re-run the P-0018 shape (two contracts in `cli.py`, one in `policy.py`) through the dry run and check the lanes it prints.

## Left to the builder

- How lanes are represented in the scheduler (a list of contract ids per lane is enough).
- The rebase BUILD brief's wording.
- Where the lane listing appears in `ateam status`.
