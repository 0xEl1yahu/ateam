# ATeam backlog

One list of what we want next, so ideas stop living in chat. Evidence and design stay in the linked sources; this file only says what the item is, why we want it, and whose decision it is. When a plan picks an item up, write its plan id beside it; when the plan merges, delete the row.

"Owner's call" follows CLAUDE.md: anything that organises work, changes what a worker may do, changes a gate, a transition or what counts as done, or widens or narrows the policy. Everything else is mechanics.

Sources: the harness spec (`docs/superpowers/specs/2026-10-24-ateam-harness-design.md`), the dogfood findings (`docs/dogfood/2026-10-27-first-self-runs.md`), the runtime write-up (`docs/2026-10-25-product-and-future-runtime.md`).

## Next: the six-area synced trial

Planned together as one plan in the sync format, so the areas agree on the event and log shapes before any is built.

| Area | What | Source | Owner's call? |
|---|---|---|---|
| Event log | One append-only `logs/<attempt-id>.jsonl` per attempt, written as the SDK stream arrives; events addressed `A-<attempt>#<seq>` | Spec, "Observability and provenance" | Decided (owner, 2026-10-25) |
| Watch and log | `ateam watch P-xxxx`: tmux, one window per running attempt tailing its log, a status window, a pauses window. `ateam log A-0042 [--type] [--since]` without tmux. Closes dogfood V1 | Spec, same section; owner asked 2026-10-29 | Decided |
| Doctor repair | `ateam doctor --fix` (auto class only), `--propose`, `--rebuild` | Spec, "Doctor and medic" | Decided; each new auto-class fix is mechanics |
| Calibration | Compare predicted complexity, risk and uncertainty with actual tokens, retries, time and outcome; suggest tier changes | Spec, "Calibration" | Yes, for any change to the tier rules |
| Abandon | `paused -> abandoned` for a plan, from the CLI | Spec, plan state machine | Yes: a state-machine edge |
| Worktree cleanup | Remove a plan's worktrees and branches after merge or reject (~337 MB each). Done by hand today. Closes dogfood C2 | Spec, "Workers run in isolated git worktrees" | Yes: when it runs automatically |

Also in the spec's pass two: `ateam lessons` (proposed records and policy changes with the events behind them). Fits after the event log.

## Speed (measured)

| # | What | Evidence | Owner's call? |
|---|---|---|---|
| S1 | Verify runs the full suite for every contract (dogfood T1). Run the contract's own checks and tests per contract, the full suite once at integration | P-0018, P-0019 (85% of session time in full-suite runs), P-0020 | Yes: changes what "verified" means |
| S2 | Verifies run one at a time behind the writer gate, so they queue | P-0020: C-0089 waited behind C-0090's verify | Yes: verify is a writer by ruling (2026-10-28) |
| S3 | A haiku BUILD ran the full `pytest -n auto` although its brief says to run the contract's own tests | P-0020 C-0095 | No (prompt or policy mechanics); a hard limit would be a worker-permission change |
| S4 | Planning effort: Claude judges the request instead of always `careful` (dogfood C1) | P-0018 $5.58, P-0019 $7.19, P-0020 $6.21 | Yes: changes the 2026-10-25 ruling |

## Fixes found in runs

| # | What | Evidence | Owner's call? |
|---|---|---|---|
| X5 | A crash right after the scheduler's rebase-merge commit leaves the BUILD attempt `scope_breach` and the old checkpoint, so TEST's brief lists other contracts' merge-staged files. Repeat the amend and checkpoint on re-entry | P-0020 review | No |
| X6 | `ateam status` says a resolved, staged rebase merge is "still open", though the run will conclude it and start at TEST | P-0020 review | No |
| X7 | Conflict-marker detection assumes 7 characters; a `conflict-marker-size` attribute or a seven-`=` underline gives a false pause (fails safe) | P-0020 review | No |
| X8 | After a verify pause fixed outside the code, a re-run always spends a BUILD, because the failure is recorded on that HEAD | P-0020 review (as specified in C-0094) | Yes: what a resume skips |
| X2 | A ResultMessage whose `total_cost_usd` is None still records $0; it should be unknown, like a missing result | P-0020 planning, open question 7 | No |
| X3 | `status` offers `resume --note` for a final-integration pause, which `resume` refuses (dogfood V2) | P-0018 | No |
| X4 | Worker sessions show as messageable peers in `ListAgents` (dogfood V3). Check, and turn off in the clean room if real | P-0018 | Possibly: a clean-room change |

## Records hygiene

| # | What | Evidence | Owner's call? |
|---|---|---|---|
| H1 | Check proposed records against the code before approval (dogfood P2) | R-0014, R-0020 described code never built | Approval stays the owner's |
| H2 | Doctor warns "N proposed records, none approved" (dogfood P3) | All 20 records sat proposed for days | No |

## Later runtime: Go and Rust

Python stays the only writer of the intent repo and `state.db`, the only SDK caller and the only git client. A second language arrives when a measurement says so, never by preference (runtime write-up, "Later runtime").

| Piece | Language | Starts when | Evidence so far | Owner's call? |
|---|---|---|---|---|
| `ateam-scan`: secrets and API-surface scanners on a diff, wired as named checks | Rust | A real target has tokens or routes worth failing a contract over | None yet | Yes: new check family |
| `ateam-scan serve`: long-lived scan heap with a memory ceiling | Rust | A one-shot scan is measured too slow or heavy per contract | None yet | No, once `ateam-scan` exists |
| `ateam-supervise`: spawn, timeout, cancel, worker cap; one process group per worker | Go | Stop, cancel, leaked processes or idle coordinator memory fail on a real run | P-0019: one worker's `pkill` killed others' sessions. The policy now blocks it (aa0370c); OS-level isolation would contain it too | Yes |
| Tools workers can call (for example a Rust MCP server) | Rust | A worker needs a capability the shell and file tools cannot give it | None yet | Yes: workers run with an empty, strict MCP config today; any tool is a worker-permission change |

Out of scope by the write-up: a Rust "memory" for agents. Agent memory is approved records plus git; a transcript store would be a second memory with no approval step.

## Deferred until a real failure shows the need

From the spec: token-budget allocation, context-pressure detection and session rotation, semantic retrieval or embeddings, summarisation hierarchies, live context sync between concurrent workers, custom compression. Auto-memory stays off for workers.
