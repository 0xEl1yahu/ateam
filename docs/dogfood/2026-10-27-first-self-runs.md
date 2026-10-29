# Dogfood findings: first self-runs (2026-10-27)

Sources:
- the live pause/resume test (scratch repo);
- P-0018: four contracts planned and run by ATeam on ATeam;
- P-0019 (2026-10-28): eight contracts, sync planning, run with lanes and the overlap gate;
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
| F6 | T2: a verified contract waited for running sessions before it could merge | P-0018 run log | ddb3e82: merges and verify are writers that run beside sessions; a session's intent check accepts the `ateam:` commits ATeam recorded (owner, 2026-10-28) |
| F7 | T3/T4: capacity idle; worktree preparation and merges waited for every running session | P-0018 and P-0019 run logs | 0649102 lanes; 5f549b7 `max_workers` 8; ddb3e82 worktree prep under a refs lock, not the gate |
| F8 | One worker's `pkill -f pytest` killed other workers' sessions (exit 143), costing two attempts | P-0019 run log | aa0370c: the policy denies `pkill`, `killall` and `kill` of anything but a numeric PID (owner, 2026-10-28) |
| F9 | A contract paused `budget_exhausted` because the write policy left out its `incidental` paths | P-0019 C-0083 | 8a4312c: scope is `paths` plus `incidental` in the write policy too |

## Open: wall-clock time

Most of the time in P-0018 went to waiting, not working: 2 h 10 min wall clock against about 45 min of sessions.

| # | Finding | Evidence | Proposed fix | Owner's call? |
|---|---|---|---|---|
| T1 | Verify runs the full suite for every contract, and nothing else may run meanwhile | Each verify took 4-9 min. Final integrate took 9 min. Four contracts meant four full suites back to back | Per-contract verify runs the contract's own checks plus the tests its acceptance names. The full `config.checks` runs once, at integration (it already does). Needs a way to mark a config check "integration only". Since P-0019, BUILD and TEST run the contract's own tests; verify still runs the full checks | Yes: changes what "verified" means per contract |

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
| C2 | Each contract worktree is ~337 MB (its own `.venv`) and is never removed | `du` of `~/.ateam/.../worktrees` | A cleanup command after merge or reject, already deferred in the spec. Lanes cut it to one worktree per lane; setup still runs on each new base | No |
| C3 | Haiku's pause diagnosis misjudged the class and said setup needs a re-freeze | Live test findings | None needed: real pause investigations run at `careful`. Keep in mind if triage lowers tiers | No |

## Open: found in P-0019, planned as P-0020

| # | Finding | Evidence | Fix in P-0020 |
|---|---|---|---|
| R1 | A rebase BUILD resolved and staged the merge but did not commit it; the plan paused `environment_failure` and the attempt read `scope_breach` | P-0019 C-0088 | The scheduler concludes a fully resolved, in-scope rebase merge itself |
| R2 | Resume re-runs BUILD even when the contract's TEST already passed on its current head | P-0019 resumes | A per-commit record of passed steps; resume starts after the last one |
| R3 | A session killed before its result message is recorded at $0 | P-0019 sessions killed by `pkill` | No result means unknown cost, counted at the tier cap |
| P4 | Planning dropped distinct record proposals as duplicates because they cited the same evidence | P-0019 planning output | Duplicates match on evidence plus statement; a rejected record still suppresses by evidence alone |

## Open: process lessons

| # | Lesson | Where it should live |
|---|---|---|
| P1 | Tests with wall-clock deadlines must be generous and assert their precondition, or they pass alone and fail under a loaded suite | A CLAUDE.md conventions line |
| P2 | Records discovered from a stale plan described code that was never built (R-0014, R-0020). Proposed records should be checked against the code before approval | Records review |
| P3 | All 20 ATeam records sat `proposed` for days, so no binding record reached a worker until today | Nothing to fix in code: approval is the owner's gate. Worth a doctor warning ("N proposed records, none approved") |
