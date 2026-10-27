# Pause, investigate and resume: design

Date: 2026-10-26. Status: approved by the owner 2026-10-26; decisions 11-13 added at plan review the same day. Builds on `ateam run` (the scheduler, plan `docs/superpowers/plans/2026-10-26-scheduler.md`) and amends the binding harness design (`docs/superpowers/specs/2026-10-24-ateam-harness-design.md`).

## Intent

Today a pause is a dead end. `ateam run` stops a plan with a pause reason, then refuses to run it again, and nothing resumes or abandons it. The first real dogfood run will pause, and the owner will have no move except editing the state store by hand.

This design gives a pause a way out that keeps the owner in charge. When a run goes wrong, the owner wants to stop it, understand what happened, and send the next worker back in with guidance. The understanding comes from one of two places: an INVESTIGATE session that ATeam starts by itself and that hands a suggestion to the owner, or the owner reading the worktree. Either way, nothing continues until the owner says so, and nothing the owner or the investigator writes changes what "done" means.

## Goals

- The owner can pause a running plan from another terminal (`ateam stop`).
- When ATeam pauses a plan on its own, a read-only INVESTIGATE session on each paused contract starts automatically and leaves findings plus a suggested instruction for the owner.
- The owner can resume a paused plan (`ateam resume`), optionally with an instruction for the next BUILD, either written by the owner or adopted from the investigator's suggestion.
- A resumed contract gets its frozen BUILD attempts again.
- `ateam status` shows a paused plan's reason, the paused contracts, the findings, the suggestions and the commands to continue.
- A pause reason tells the truth: verify refusals that are not decision changes stop classifying as `decision_change_needed` (ledger item #17 of the scheduler branch).

## Non-goals

- Event logs, `ateam watch`, doctor and medic. A stale lock is still removed by hand.
- `serialize_checks`, the rebase BUILD after a merge conflict *(built 2026-10-27 by `2026-10-27-lanes-design.md`)*, INVESTIGATE before BUILD for uncertain contracts, downstream revalidation, and native session resume by ID. They stay recorded deviations.
- Investigating a pause that happens during final integration (`integrating -> paused`).
- Abandoning a plan (`paused -> abandoned`). The transition exists; a command for it is separate work.

## Anti-goals (failure even if it works)

- **Guidance that redefines success.** An instruction never changes a contract, its acceptance criteria, its paths or a record. If the right fix needs one of those, the route is the existing one: a record approval or a re-freeze.
- **The investigator acting on its own conclusion.** Its suggestion reaches a worker only after the owner adopts it. It never resumes anything.
- **TEST anchored by a hint.** TEST never sees an instruction, just as it never sees BUILD's report.
- **A silent resume.** Nothing returns a paused plan to `executing` except the owner running `ateam resume`.
- **Stale guidance.** An instruction written for a contract that has since been re-frozen never reaches a worker.

## Decided by the owner (2026-10-26)

1. A run can be paused by ATeam or by the owner. From a pause, the way forward is an INVESTIGATE session, the owner's own inspection, or both, followed by the owner passing instructions back.
2. Instructions are guidance within the frozen contract ("use the existing helper in X"). They do not change what the contract asks for.
3. On resume, a paused contract's BUILD attempt budget is refreshed to the frozen `budget.attempts`, as if fresh.
4. When ATeam pauses on its own, the INVESTIGATE session starts automatically.
5. The investigator presents its suggestion to the owner. ATeam does not adopt it or resume by itself.
6. **A pause changes no contract status.** The plan goes to `paused` as today; which contracts paused is recorded in the store's pause record, and resume writes a store row that opens each paused contract's new attempt window. No new contract transitions and no intent commits for pausing or resuming a contract.
7. **Resume refuses a merge-conflict pause** (a contract's merge onto `ateam/<plan>`, or integration's merge of a moved target), naming the pause, until the rebase BUILD exists. *Superseded 2026-10-27 by `2026-10-27-lanes-design.md`: a contract's conflict pause resumes into the rebase BUILD; only a moved-target conflict, which names no contract, is still refused.*
8. **Resume does not gate on the pause reason.** It applies the existing rule that every unfinished contract must bind (frozen, unchanged since the freeze, runnable, no record drift). The owner running `resume` is the judgment that the cause is dealt with.
9. **Instructions are tied to the contract they were written for.** Each records the contract's frozen hash at the time; after a re-freeze changes the hash, the instruction stays in the file for history but is no longer delivered.
10. **Pause investigations run in the paused contract's worktree**, read-only, with the full snapshot (HEAD, every ref, the stash list, dirty paths and their content, and the intent repo), not narrowed. A change discards the findings as `tamper` and the pause stands. This amends the rule that read-only investigations run in the owner's checkout (CLAUDE.md rule 6 and the spec's carve-out), for pause investigations only.
11. **A worker may read its own worktree under ATeam's home.** Scheduler worktrees live at `<home>/worktrees/<cid>`, and the home-read guard denied a worker its own files there. A path that resolves inside the worker's own worktree is not "ATeam's home". Nothing else under the home becomes readable: not other worktrees, not the integration worktree, not the intent repo, the state store or other scratch directories.
12. **Resume refreshes only the contracts a pause record lists.** After `owner_stopped` none are refreshed, since interrupted attempts already do not count. A plan paused without a record refreshes none, and resume says so.
13. **Planning and running exclude each other.** While a run holds the lock, every owner command that commits to the intent repo refuses (`init`, `plan`, `approve`, `reject`, the `records` lifecycle commands, `contracts freeze`, `merge`, `intent pull`, `resume`, and a direct `stop`). While a planning session is live, `run`, `resume` and a direct `stop` refuse. The commands the scheduler runs as children are not guarded.

## Calls made by the assistant (owner to check)

- **New pause reason `owner_stopped`.** The spec's enum has no reason that means "the owner stopped it"; reusing another would misreport. Added to the enum.
- **Ctrl-C keeps its current meaning.** Ctrl-C in the terminal running `ateam run` interrupts, leaves the plan `executing`, and a re-run continues (existing owner ruling). `ateam stop` is the deliberate pause.
- **Escalation resets with the attempt budget.** After a resume, the contract starts again from its frozen BUILD tier and may escalate once more.
- **Instructions live in the intent repo.** They are intent the owner wrote: one YAML file per contract, `intent/guidance/C-xxxx.yaml`, committed with the `ateam:` prefix by `ateam resume`. The store only references them.
- **Instructions accumulate.** Every later BUILD attempt of that contract receives all of the contract's live instructions (those whose hash matches the current freeze), oldest first, each dated and marked owner-written or adopted from investigation `A-<attempt-id>`.
- **Findings live in ATeam's home, not the intent repo.** They are a worker's unverified report, stored as the INVESTIGATE attempt's structured output and read through `ateam status`.
- **Investigations run at `careful`**, following the owner's standing rule that investigations default to `careful`, with the cap reserved against `run_budget_usd` like any session. If the reservation does not fit, the investigation does not run and the pause says so.
- **One investigation per paused contract.** A pause with no contract (final integration) is not investigated.
- **`ateam resume` resumes and runs.** It records any instruction, flips the plan back to `executing`, and continues in the same process as `ateam run` would. `--no-run` stops after the state change.
- **`ateam stop` works through the store**, not signals: it records a stop request the running scheduler polls in its main loop. The scheduler then interrupts only the child sessions it launched, by pid. It never signals a process group, because a stale lock's reused pid, or a run started from a script, would put unrelated processes in that group. With no live run, `stop` pauses an `executing` plan directly while holding the run lock.
- **Refusals that are not drift do not report drift (#17).** Record drift pauses with `records_drifted`. Any other contract that no longer binds (a hand edit after freeze, a file/store mismatch) pauses with `verification_unresolvable`. A refusal from the run's own environment pauses with `environment_failure`.
- **An instruction names its contract when there is a choice.** With one paused contract, `--note` and `--adopt` apply to it. With several, `--adopt` adopts each contract's own suggestion, and `--note` needs `--contract C-xxxx` (repeatable). After `owner_stopped`, no contract is paused, so `--note` needs `--contract`.
- **Resume after a non-conflict integration pause** (setup failure, failed final checks) re-runs integration; instructions are refused there, since no contract is paused.

## How it works

### Pausing

ATeam pauses as it does today: a pipeline returns a pause, no new stage starts, running stages finish, and the plan goes `executing -> paused` with the first reason. In addition, the scheduler writes a pause record to the store: plan, reason and detail, and each paused contract with its own reason and detail and its frozen contract hash at the time. Contract statuses do not change.

`ateam stop P-xxxx` records a stop request for the plan. The running scheduler sees it within its loop, interrupts running sessions (their attempts end `interrupted` and do not count), and pauses the plan with `owner_stopped: stopped by the owner`; the pause record lists no contracts. If no run holds the lock, `stop` pauses an `executing` plan directly. `stop` on a plan that is not `executing` is refused with its status.

### Investigating

After a pause ATeam started (any reason except `owner_stopped`), the scheduler marks the plan paused first, then launches one INVESTIGATE session per paused contract before the run returns:

- **Where:** the contract's worktree, read-only (the INVESTIGATE tool set and shell allow-list), with the full snapshot before and after. Any change marks the attempt `tamper`, its findings are discarded, and the pause stands.
- **Brief:** Binding and Task sections as BUILD would receive them; the pause reason and its evidence (failing check output, verify reasons, TEST verdicts with evidence, the error); the attempt history (role, tier, outcome, failure class per attempt); the last BUILD's report and handoff notes, labelled unverified; the contract's live instructions, labelled as instructions already given.
- **Output schema:** `summary`, `evidence` (path/line or check references with what they show), `failure_class` (`implementation`, `requirements`, `environment`, `invariant_violation`), `needs_owner_decision` (null, or a sentence when the fix needs a record change, a re-freeze or a contract change), and `suggested_instruction` (null, or one short instruction within the contract).
- **Cost:** a normal attempt row, role `investigate`, in calibration telemetry and the run's spend; never in the contract's BUILD attempts.

If the session fails, is refused by the budget, or is tampered, the pause stands and `status` says that no findings are available and why.

### What the owner sees

`ateam status P-xxxx` on a paused plan prints the reason and detail; each paused contract with its own reason, its worktree path, the findings summary, failure class and evidence, `needs_owner_decision` when set, and the suggested instruction; then the commands to continue:

```
ateam resume P-0007 --adopt                    # use the suggestion as your instruction
ateam resume P-0007 --note "use the helper in src/util/x.py"
ateam resume P-0007                            # resume with no instruction
```

A merge-conflict pause prints instead that resume is refused until the rebase BUILD exists, and which contract and paths conflict. *(Superseded 2026-10-27 by `2026-10-27-lanes-design.md`.)*

### Resuming

`ateam resume P-xxxx [--adopt | --note TEXT | --note-file PATH] [--contract C-xxxx]... [--no-run]`:

1. Refuses unless the plan is `paused` and no run holds the lock.
2. Refuses a merge-conflict pause. *(Superseded 2026-10-27 by `2026-10-27-lanes-design.md`.)*
3. Refuses unless every unfinished contract of the plan binds (`require_binding`), naming each contract that does not and why.
4. Refuses `--adopt` when a paused contract has no suggestion, `--adopt` together with `--note`, and an instruction whose contract is ambiguous (see calls above).
5. With an instruction, appends it to `intent/guidance/<contract-id>.yaml` with the date, source (owner, or the investigation's attempt id) and the contract's current frozen hash, and commits (`ateam: guidance for C-xxxx`). Refuses when the intent repo is dirty, like every intent commit.
6. Writes, for each paused contract, an attempt-window row to the store, and moves the plan `paused -> executing`. Intent repo first, store second, as everywhere.
7. Unless `--no-run`, continues as `ateam run P-xxxx`.

`ateam run` on a paused plan still refuses, naming the reason, and now also names `ateam resume P-xxxx`.

### Attempt window

The scheduler counts a contract's BUILD attempts, and decides whether it has escalated, only from attempts recorded after the contract's most recent attempt-window row, ordered by row id, not by timestamp. Interrupted attempts still do not count. Attempts before the window stay in the store for history and calibration.

### Delivering instructions

BUILD briefs gain a section between "Verified prior work" and "Previous worker's notes (unverified)", carrying only instructions whose recorded hash matches the contract's current frozen hash:

```
## Owner's instructions
Guidance from the owner for meeting this contract. It does not change the
contract or any binding record; if it ever seems to, the contract and records win.
- 2026-10-27, owner: use the existing helper in src/util/x.py
- 2026-10-27, adopted from investigation A-0042: ...
```

TEST briefs never include it.

### Pause reason fix (#17)

A verify refusal caused by record drift classifies as `records_drifted`. A verify refusal caused by the run's own environment (dirty intent repo, missing checks) classifies as `environment_failure`. `decision_change_needed` is kept for a binding decision that must change.

## Spec and CLAUDE.md changes on approval

- Pause reasons enum: add `owner_stopped`.
- Replace the deviation "a paused plan is refused by `ateam run`, naming its reason, instead of being resumed" with this design; the other scheduler deviations stand, and add "resume refuses a merge-conflict pause until the rebase BUILD exists". *(Superseded 2026-10-27 by `2026-10-27-lanes-design.md`.)*
- Amend the in-place investigation carve-out (spec "Decisions made by the owner", CLAUDE.md rule 6): pause investigations run read-only in the paused contract's worktree with the full snapshot.
- Record decisions 1-13 under "Decisions made by the owner" and the calls under "Calls made by the assistant".

## Verification the builder must plan

- Table-driven tests for resume refusals: not paused, live lock, merge-conflict pause, a non-binding unfinished contract, dirty intent repo, `--adopt` without a suggestion, ambiguous `--note`.
- Scheduler tests with injected fake sessions: an automatic pause launches exactly one INVESTIGATE per paused contract and none for `owner_stopped`; the plan is paused before the investigation starts; the attempt window refreshes the budget and the escalation; TEST briefs never carry instructions; an instruction stops being delivered after a re-freeze; a tampered investigation leaves the pause standing with no findings.
- Classification tests for #17.
- `ateam stop` from another process against a running scheduler (store request observed, attempts interrupted, plan paused `owner_stopped`).
- One opt-in live run: a scratch target whose contract fails until a hint is given; pause, investigate, resume with `--adopt` or `--note`, reach `merge_proposed`.

## Left to the builder

- The store schema for stop requests, pause records, attempt windows and findings references.
- The exact wording of `status`, refusals and the investigation brief.
- Whether `ateam stop` waits for the run to acknowledge, and for how long.
- The guidance YAML's fields beyond date, source, contract hash and text.
