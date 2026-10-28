# Starts and merges overlap running sessions: design

Date: 2026-10-28. Status: direction approved by the owner in chat ("We can remove limit of max agents, and also, fold those changes in too", 2026-10-28). Amends the scheduler's serialisation rule (`docs/superpowers/specs/2026-10-24-ateam-harness-design.md`, "`ateam run` (the scheduler)"; record R-0018) and the worker session's tamper check.

## Intent

The owner wants a plan's lanes to run asynchronously. P-0019, the second self-dogfood, showed why they mostly do not. Every step that writes ATeam's state takes the scheduler's exclusive gate, which waits for all running sessions to finish and holds new ones back while it waits. Those steps include preparing a contract's worktree, refreshing contracts, verify, merge-at-verify and plan status.

So a contract that becomes ready while another session runs waits for that session to end, with a worker slot idle (finding T4). A verified contract waits the same way before it can merge (T2, C-0079 in P-0018: 19 minutes). Two contracts run in parallel only when they start in the same instant. A higher `max_workers` buys almost nothing until this changes.

## Goals

- Preparing a contract's worktree, its first step, does not wait for running sessions.
- Merge-at-verify and the intent commits around it do not wait for running sessions.
- A running session's tamper check still fails on any change a worker could make, and passes on changes the coordinator itself made.
- `max_workers` defaults to 8 for this target.

## Non-goals

- Running two verifies at once, or a verify beside sessions. Verify still runs alone, because its snapshot covers the state it judges.
- Changing what counts as tamper for verify, or for the read-only investigations of `ateam init`, `ateam plan` and pause investigations (rule 6).
- A new store table, record kind or module.

## Anti-goals

- **A weaker tamper check.** Whatever a worker could change, by accident or not, is still caught. Only changes the coordinator made and recorded are accepted.
- **Two writers at once.** Writers (refresh, verify, merge, plan status) stay serialised among themselves by the gate.

## Facts from the code (2026-10-28)

- A worker session's tamper check (`runner/run.py:191-196`) compares only the intent repo: HEAD before and after, and that it is clean after.
- Verify's snapshot (`verify.snapshot` and `compare`) covers:
  - the worktree's HEAD and tracked files;
  - the intent repo's HEAD and dirty files;
  - the owner checkout's HEAD;
  - the state store's fingerprint for its own contract only, plus record hashes. Other contracts' rows are excluded (`StateDB.fingerprint`).
- Preparing a worktree (`worktrees.contract_worktree`, under the gate at `scheduler.py:637`) touches none of these. It writes the new contract's `base:` and `lane:` meta keys and creates a branch and a linked worktree in the owner's repository, not its checkout's HEAD.

## Decided by the owner (2026-10-28)

1. **Starts and merges overlap running sessions.** The tamper check of a running session accepts changes the scheduler itself made during that session, so the gate no longer drains sessions for them.
2. **No practical worker limit.** `max_workers` is raised; see the call below for the value.

## Calls made by the assistant (owner to check)

- **Worktree preparation leaves the exclusive gate.** It holds a separate lock that serialises git ref and worktree creation among ATeam's own threads, and it never waits for sessions. The leftover-files check for a lane's later contract stays with it. Nothing in either tamper check covers what it writes, per the facts above.
- **Intent commits made during sessions** are accepted like this:
  - The coordinator records each intent commit it makes, its sha, in the state store under a meta key for the run, before releasing the lock that serialises writers.
  - A worker session's check then passes when the intent HEAD after the session descends from the HEAD before it, and every commit in between is one the coordinator recorded.
  - Anything else is still tamper: an unrecorded commit, a rewritten history, or a dirty intent repo.
  - The after-check takes the writer lock in shared mode, or retries, so it never reads the intent repo mid-commit. The builder chooses which.
- **Which steps keep draining sessions.** Verify does: its snapshot is about the contract's committed work and must not see its own worktree move, so it still runs alone. Merge-at-verify, contracts refresh and plan status become writers that wait only for other writers.
- **`max_workers` becomes 8** in the target config and in the default. It's an integer guard, not a design limit. With targeted test runs in sessions (P-0019), eight sessions do not each run the full suite.
- **Pause investigations, `ateam init` and `ateam plan` are unchanged.** They run outside `ateam run`, and their ref snapshots (rule 6) keep their meaning.

## Verification the builder must plan

- A scheduler test with fake sessions: contract B becomes ready while A's BUILD runs, and B's BUILD starts before A's ends.
- A scheduler test: a verified contract merges while another contract's BUILD runs, and that BUILD's attempt ends `completed`, not `tamper`.
- Table-driven tests for the worker session's intent check:
  - unchanged HEAD;
  - one recorded coordinator commit;
  - two recorded commits;
  - an unrecorded commit (tamper);
  - a recorded commit plus an unrecorded one (tamper);
  - a non-descendant HEAD (tamper);
  - a dirty intent repo after the session (tamper).
- Verify still drains sessions: a test that a verify never overlaps a session.
- The existing gate tests (writer preference, the stop path) still pass, or are updated with a comment naming this design.

## Left to the builder

- The lock's shape (a second lock, or a mode of the Gate).
- The meta key's name and how long recorded shas are kept.
- Whether the after-check takes a shared lock or retries.
