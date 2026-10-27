# Dogfood findings: first self-runs (2026-10-27)

Sources:
- the live pause/resume test (scratch repo);
- P-0018: four contracts planned and run by ATeam on ATeam;
- the session log of both.

Each entry gives the evidence, the effect, a proposed fix, and whether the fix is the owner's call under CLAUDE.md.

## Fixed already

| # | Finding | Evidence | Fix |
|---|---|---|---|
| F1 | Every real worker was denied its own worktree, because scheduler worktrees sit under ATeam's home | Staff review of the pause/resume plan; probe | Task 0a, own-worktree exemption (owner ruling) |
| F2 | `ateam run` looped forever when a BUILD failed before recording an attempt (setup failure) | Live test: ~1,980 launches in 15 min | dd1072d: a setup failure pauses at once; the loop counts its own launches |
| F3 | The pause investigator could not read the failed step's log under ATeam's home | Live test findings: "the build log is blocked" | P-0018 C-0077: ATeam puts the log tail into the brief |
| F4 | A flaky test (10 s wall-clock deadline) failed P-0018's final check under load | Integrate log: IndexError at test_run_command.py:308 | 079d35e: a 120 s wait plus an explicit precondition |
| F5 | A `SyntaxWarning` in every run log | `tests/test_policy.py:109`, `"\;"` | 079d35e: raw string |

## Open: wall-clock time

Most of the time in P-0018 went to waiting, not working: 2 h 10 min wall clock against about 45 min of sessions.

| # | Finding | Evidence | Proposed fix | Owner's call? |
|---|---|---|---|---|
| T1 | Verify runs the full suite for every contract, and nothing else may run meanwhile | Each verify took 4-9 min. Final integrate took 9 min. Four contracts meant four full suites back to back | Per-contract verify runs the contract's own checks plus the tests its acceptance names. The full `config.checks` runs once, at integration (it already does). Needs a way to mark a config check "integration only" | Yes: changes what "verified" means per contract |
| T2 | A verified contract waits for running sessions before it can merge | C-0079 was verified at 15:25 and merged at 15:44, because C-0080's TEST held the gate | Merge-at-verify commits to the intent repo, which would trip running sessions' tamper check. Option: the tamper check accepts intent commits the coordinator itself recorded during the session, so merges and sessions can overlap | Yes: changes a tamper check |
| T3 | Two workers left capacity idle: C-0079 and C-0080 waited ~50 min while C-0078's Opus BUILD (26 min) and the verifies ran | Run log timestamps | Lanes (being built) plus T1 and T2; maybe raise `max_workers` | No for `max_workers`; yes for T1 and T2 |

## Open: visibility

| # | Finding | Evidence | Proposed fix | Owner's call? |
|---|---|---|---|---|
| V1 | No live progress view. Following the run took raw `sqlite3` queries on `state.db` and tailing a log file | This session's heartbeats | `ateam status P` shows open attempts with elapsed time, the current step (BUILD, TEST, verify, waiting at the gate) and spend so far. That is the spec's deferred event log and `watch`, in small form | No |
| V2 | `status` offers `ateam resume --note` for a pause during final integration, which `resume` then refuses | `ateam status P-0018` output | Offer only the commands resume accepts for that pause (deferred-minor class) | No |
| V3 | Worker sessions show as interactive, messageable peers (`c-0078-05`, `c-0079-ef`, `c-0080-4a`) | `ListAgents` during the run | Check whether a headless worker can receive cross-session messages. If so, turn that off in the clean-room options: it bypasses the brief and the policy | Possibly: a clean-room change |

## Open: cost and resources

| # | Finding | Evidence | Proposed fix | Owner's call? |
|---|---|---|---|---|
| C1 | Planning cost $5.58 at `careful` for four small changes; the whole run cost $3.13 | `ateam plan` output | Claude triage of planning effort (request drafted; project memory note) | Yes: changes the 2025-10-25 ruling |
| C2 | Each contract worktree is ~337 MB (its own `.venv`) and is never removed | `du` of `~/.ateam/.../worktrees` | A cleanup command after merge or reject, already deferred in the spec. Lanes cut setups per plan | No |
| C3 | Haiku's pause diagnosis misjudged the class and said setup needs a re-freeze | Live test findings | None needed: real pause investigations run at `careful`. Keep in mind if triage lowers tiers | No |

## Open: process lessons

| # | Lesson | Where it should live |
|---|---|---|
| P1 | Tests with wall-clock deadlines must be generous and assert their precondition, or they pass alone and fail under a loaded suite | A CLAUDE.md conventions line |
| P2 | Records discovered from a stale plan described code that was never built (R-0014, R-0020). Proposed records should be checked against the code before approval | Records review |
| P3 | All 20 ATeam records sat `proposed` for days, so no binding record reached a worker until today | Nothing to fix in code: approval is the owner's gate. Worth a doctor warning ("N proposed records, none approved") |
