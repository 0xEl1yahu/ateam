# queue-switchclaude integration research

Date: 2026-10-25  
Status: research and recommendations; not an approved implementation plan  
ATeam baseline: `84fd25f`  
Upstream revision reviewed: `f23e536edfdcb31a6f794ec1df7bc528808ae0fe`

## Recommendation

Integrate the rules-first message classifier at ATeam's coordinator boundary. Reuse its decision vocabulary, classification logic, prompt, and evaluation fixtures. Keep ATeam as the sole authority for planning, contracts, execution, verification, and merge approval.

Start in shadow mode: classify and record messages without changing active execution. Pair this with the evaluation facility described in [the autoresearch integration research](2026-10-25-autoresearchospy-integration-research.md).

## What upstream implements

[queue-switchclaude](https://github.com/0xEl1yahu/queue-switchclaude) is an interactive Claude Code plugin for sorting messages into `steer`, `queue`, `amend`, and `split`, with interrupt and parked flags. It manages dependency ordering, planning, approval, handoffs, and completion through Claude hooks and per-session files. It is not an account-switching tool.

Rules handle explicit markers; ambiguous messages use an isolated `claude -p` classifier. The repository includes a 47-case golden intake dataset. The README reports 44/47 for cached model decisions; that is upstream evidence, not a result independently reproduced in this research.

Sources: [classifier](https://github.com/0xEl1yahu/queue-switchclaude/blob/f23e536edfdcb31a6f794ec1df7bc528808ae0fe/scripts/qsc/classify.py), [model wrapper](https://github.com/0xEl1yahu/queue-switchclaude/blob/f23e536edfdcb31a6f794ec1df7bc528808ae0fe/scripts/qsc/model.py), [README](https://github.com/0xEl1yahu/queue-switchclaude/blob/f23e536edfdcb31a6f794ec1df7bc528808ae0fe/README.md).

## Proposed architecture

```text
Incoming message + coordinator context
                  |
          Rules / model classifier
                  |
          Validated intake proposal
                  |
         ATeam request and plan services
                  |
        Owner approval / frozen contracts
                  |
         ATeam scheduler and workers
                  |
         Verification / merge approval
```

The classifier returns a proposal; it does not grant permissions, approve records, freeze contracts, or mark work complete. Operational queue state belongs in ATeam's state store. Durable requests and approved intent belong in its private intent repository. Avoid a second authoritative queue in plugin session files.

## Decision mapping

| Classification | Proposed ATeam handling |
|---|---|
| `queue` | Save a request and proposed ordering; route through planning and approval |
| `parked` | Save a backlog item excluded from automatic dispatch |
| `amend` | Propose a revision to an explicitly identified request or plan |
| `steer` | Check whether the message clarifies the frozen contract or changes scope |
| `split` | Preserve the original message and record distinct linked proposals |
| `interrupt` | Request a controlled stop and checkpoint |
| Unclear or classifier failure | Retain the message as unresolved; do not mutate an active contract |

For the first version, record steering for coordinator review rather than inject it into active SDK sessions. Scope-changing messages must pass through revised planning and approval. Live steering and interruption require a separate runtime spike and owner-approved semantics.

## Existing integration points

| ATeam file | Role in integration |
|---|---|
| `src/ateam/cli.py` | Initial explicit intake and queue inspection commands |
| `src/ateam/planner.py` | Convert requests into validated draft plans and contracts |
| `src/ateam/plans/model.py` | Existing plan identity, history, and plan dependencies |
| `src/ateam/contracts/model.py` | Contract dependencies and immutable execution scope |
| `src/ateam/state/db.py` | Operational state and future dispatch coordination |
| `src/ateam/runner/binding.py` | Preserve frozen-contract and record-drift checks |
| `src/ateam/runner/options.py` | Preserve clean-room SDK options and ATeam policy hooks |
| `src/ateam/checkpoint.py` | Existing checkpoint data for eventual controlled interruption |

ATeam currently lacks the automatic scheduler, worktree pool, verifier, and integrator. Intake can be built before them, but full automatic dispatch depends on those stages.

## Behaviours that must not be copied unchanged

### Completion detection

Upstream's judge fails open: an unavailable or invalid judge can return `done`. Repeated incomplete work can close with `gave_up`, permitting dependent work to proceed. ATeam must release engineering dependencies only when the applicable verification gate succeeds. A worker's closing prose is not sufficient evidence.

Sources: [done.py](https://github.com/0xEl1yahu/queue-switchclaude/blob/f23e536edfdcb31a6f794ec1df7bc528808ae0fe/scripts/qsc/done.py), [dispatch.py](https://github.com/0xEl1yahu/queue-switchclaude/blob/f23e536edfdcb31a6f794ec1df7bc528808ae0fe/scripts/qsc/dispatch.py).

### Cancellation and dependencies

Upstream treats dependencies as ordering and rewires around cancelled, dropped, or interrupted tasks. ATeam dependencies can represent required artifacts or verified implementation. Recommend blocking affected dependants pending replanning rather than silently bypassing the missing work. Unknown dependency identifiers also need explicit validation.

Source: [store.py](https://github.com/0xEl1yahu/queue-switchclaude/blob/f23e536edfdcb31a6f794ec1df7bc528808ae0fe/scripts/qsc/store.py).

### Approval and steering

Conversational approval words must not bypass ATeam's explicit plan/record approval services. Classifier failure must not automatically deliver an unvalidated scope change to a worker. Bind approval actions to the specific plan revision being reviewed.

### Hook and runtime assumptions

Installing the plugin does not establish integration with ATeam's headless SDK workers. ATeam supplies its own hooks, disables slash commands, and uses no settings sources. Port the intake capability behind a Python interface instead of importing the interactive Stop-hook dispatcher.

### Verification commands

Upstream accepts task-specific shell verification commands. ATeam should retain its configured named checks and independent TEST role, rather than accept arbitrary classifier-produced commands.

## Suggested implementation order

1. Add `src/ateam/intake/model.py`, `rules.py`, `classifier.py`, and `service.py` with typed decisions and traceable original messages.
2. Adapt upstream labelled examples and add ATeam cases for frozen contracts, stale approvals, missing dependencies, and record changes.
3. Run shadow intake through explicit CLI entry points; record classification, latency, uncertainty, and disagreements.
4. Connect accepted intake proposals to ATeam's existing planning services and private intent storage.
5. Add dispatch after scheduler and verifier implementation, with transactional claims and restart recovery.
6. Consider live steering and interruption only after a targeted SDK runtime spike.

These paths and interfaces are proposals. ATeam has no existing intake module to configure today.

## Validation gates

- Duplicate messages cannot create duplicate execution.
- Unknown amendment targets and dependency cycles are rejected or held unresolved.
- A cancelled prerequisite cannot silently release dependent engineering work.
- Classifier errors cannot mutate frozen contracts or approve plans.
- An approval applies only to the reviewed revision.
- A failed or unavailable verifier cannot produce verified completion.
- Restarting the coordinator cannot dispatch an already claimed item twice.
- Shadow mode has no effect on worker execution.

## Decisions reserved for the owner

Agree on steering boundaries, cancellation consequences, approval language, request persistence, and dispatch readiness before implementing those behaviours. The repository instructions reserve workflow and state-machine changes for the owner; this document recommends them without making them binding.

## Verification limits

Source inspection only. No plugin was installed, no model evaluation was rerun, and no SDK hook compatibility was established. Preserve upstream MIT attribution if code or evaluation assets are copied. Runtime behaviour and performance require targeted tests.
