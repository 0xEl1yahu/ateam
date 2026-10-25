# ATeam product handoff

Date: 2026-10-25. Audience: an agent in a fresh window, briefing the owner or continuing product work.

Read this file before proposing features, competitors, or a Rust or Go runtime. The full write-up of the same matters is `docs/2026-10-25-product-and-future-runtime.md`. The harness design remains the source of truth for v1 behavior: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`. This file is the short brief. It does not change the v1 spec.

## What to hand back

ATeam is a control layer around a coding agent. It keeps the rules that must stay true, freezes what "done" means before anyone writes code, and has a separate pass judge the result. OpenSpec, Spec Kit, and Kiro remember a spec for a change. Beads and Gas City remember the task list. Factory, Devin, Cursor, Codex, Copilot, and Claude Code do the work. ATeam's job is the constraint that survives the next agent.

The runnable tree on 2026-10-25 has records, contracts, config, and a version-only CLI. `ateam init`, `ateam plan`, and `ateam run` are specified and not shipped. Compare competitors to the product in the harness design, and say so when you brief.

Ship the designed loop before any new surface. The two artifacts a person should remember are the approval packet and the merge proposal. Rust scanners and a Go supervisor come after that loop exists, as extra binaries with a fixed contract. Python keeps the Claude session.

## Product

Three jobs get bundled in this market:

| Job | What the person feels | Who already does it |
|---|---|---|
| Remember the work | The agent picks up the task after a crash | Beads, Gas City, OpenSpec task lists |
| Remember the behavior | A spec describes how the system acts | OpenSpec, Spec Kit, Kiro, BMAD |
| Remember the constraint | A later agent cannot quietly undo a decision | The gap ATeam is aimed at |

ATeam serves the third job. The owner approves the plan, then approves the merge. Between those gates the tool investigates, builds, and tests, and it stops when a rule, the scope, the budget, or the checks give out.

The failure it is built for: Agent A ships under a rule. Agent B inherits the code, drops the abstraction, hardcodes the dynamic part, or reports done against a test it wrote for itself.

Durable intent in the target repo is a record. Kinds are `decision`, `invariant`, and `convention`. A record has evidence, an origin (`documented` or `inferred`), a basis (`requirement` or `observation`), a status, and the paths it binds. Workers receive approved records only. A contract freezes the acceptance criteria and the record hashes at plan approval. Roles are INVESTIGATE, BUILD, and TEST. TEST does not see the builder's report. Predicted complexity, risk, and uncertainty are stored next to what the attempt actually cost.

## Similar applications

Figures below are from public pages read on or about 12–25 October 2026. Re-check them before quoting them as current.

### Spec tools

These put a written agreement in the repo before code.

- **OpenSpec** (Fission AI). Propose, apply, archive, driven from the agent's own chat. A change holds `proposal.md`, delta specs (`ADDED` / `MODIFIED` / `REMOVED`), an optional `design.md`, and `tasks.md`. Archive folds the deltas into `openspec/specs/`, the living description of current behavior. About 38 agents. On 12 October 2026: v1.14.1, about 72k GitHub stars, on the order of 760k npm downloads that week. The person reviews the proposal. `openspec validate` runs before code. `/opsx:verify` runs after and does not block archive. Artifacts stay editable during implementation. Strongest fit for a codebase that already exists.
- **GitHub Spec Kit.** Constitution, then spec, plan, tasks, implement, plus optional clarify, checklist, and analyze. A converge step repeats until it reports converged. About 41 agent integrations. On 12 October 2026: v1.1.3, about 141k stars. Field reports describe the markdown as more process than a small change deserves. Strongest fit for a new feature that warrants a plan and a data model, or a team already on Copilot.
- **Kiro** (Amazon). Its own IDE, CLI, web, and mobile. A feature becomes `requirements.md` in EARS notation ("WHEN … THE SYSTEM SHALL …"), `design.md`, and `tasks.md`, with an approval between phases. Quick Spec can skip the approvals. Steering files (`product.md`, `tech.md`, `structure.md`, plus `AGENTS.md`) load in every session. The IDE can derive property-based checks from EARS requirements. Hooks can run a prompt or a shell command and can block a tool call. Bugfix specs are a separate, smaller path. Proprietary. Free tier with credits, paid tiers up to about $200 a month. Steering is context, and it binds only when a hook or a check fails the run.
- **BMAD-METHOD.** A simulated agile team: brief, PRD, architecture, epics, stories, then code review, adversarial review, and a retrospective. About 48 platforms. On 12 October 2026: v6.12.2, about 54k stars. The ceremony is the product.
- **loki-mode.** An issue, spec, or one line becomes a pull request. A delivery contract is written before code. Tests and a signed receipt come after. An unverified run stays a draft PR with a "NOT PROVEN" list. Claude Code by default. Business Source License 1.1 (internal use; Apache-2.0 from 2030). On 12 October 2026: v11.3.9, about 1.1k stars.
- **spec-superflow.** OpenSpec-style change folders plus one approval before code and one recorded review after (`verified` or `accepted-risk`). Small and quiet as of late September 2026.
- **Spectra.** A desktop app over the same kind of change, with discuss, analyze, verify, review, audit, and drift checks. Closed binary, free launcher.

### Work ledgers and factories

These remember tasks and run many agents.

- **Beads** (Steve Yegge / Gas City). A bead is a unit of work with status and dependencies, stored in the project, so a fresh session runs `bd ready` and continues. On mid-October 2026: about 28k stars and more than 1.7 million GitHub release downloads. The memory is the work queue. A commercial Beads Team Server adds shared ledgers and token accounting.
- **Gas Town and Gas City.** Multi-agent orchestration on Beads: formulas, molecules, handoffs, mail, parallel sessions, dashboards. Gas Town was open-sourced 1 January 2026. Gas City is the later SDK for building orchestrators. Their public framing matches ATeam's: orchestration, not a smarter prompt. The unit they orchestrate is the task.
- **Factory Missions and Droids.** The person describes the job and approves the plan. An orchestrator splits it into milestones. Workers hand off through git. A validation pass runs tests and can click through the running app. Delegations take work from Slack, Linear, Jira, and Teams. Cloud sandboxes ("Environments") and an Agent Readiness score per repo. Commercial, from about $20 a month.
- **The workers themselves.** Claude Code, Cursor (including cloud agents), Devin / Devin Desktop (Windsurf rebranded under Cognition, 2026), OpenAI Codex, GitHub Copilot coding agent. Plans, subagents, worktrees, rules files, and cloud sessions ship inside them. ATeam v1 is pointed at Claude through the Python Agent SDK and stays a layer on top of that worker.

### Review products

Sonar, CodeRabbit, and the same pattern judge a diff against general quality or architecture rules. They never see the contract this change was supposed to meet. They are useful as one more named check. They are a different product.

### Instruction files

`AGENTS.md`, `CLAUDE.md`, Kiro steering, and Spec Kit's constitution load standing rules into the agent. An ETH Zurich and LogicStar study (arXiv 2602.11988, revised 29 September 2026) found developer-written context files raised resolution about 2.4% on average, not a significant change, and added up to 19% inference cost. LLM-written files lowered resolution slightly. ATeam's bet is to select the records that bind the files in this contract, and to show that selection to the owner before the run.

## Feature comparison

| | ATeam, as designed | OpenSpec | Spec Kit | Kiro | Beads / Gas City | loki-mode | Factory Missions |
|---|---|---|---|---|---|---|---|
| What survives the session | Decisions, invariants, conventions, each with evidence, status, and the paths it binds | A behavior spec, updated by deltas | A constitution plus a folder per feature | Steering files plus a spec per feature | Tasks, dependencies, notes | A signed receipt | The PR and the mission log |
| Who may redefine done | The owner, and only before the contract freezes | The person and the agent, throughout | A checklist, then the same agent implements | The person, between phases | The agent closes the bead | A contract written before code | The person approves the plan |
| Who judges the result | A TEST pass that never sees the builder's notes, plus the checks named on the contract | Verify, and it does not stop archive | Converge, in the same session family | Property checks and hooks | Not the point of the tool | Tests and a seal; unverified stays a draft | Validation workers, including clicking the UI |
| What the owner approves | The plan, the merge, and a pause when the tool stops | The proposal, lightly | Whatever they choose to read | Requirements, design, tasks | They file the work | The PR | The plan, then the PR |
| What the worker is handed | The frozen contract and the records that bind those files | The change folder and the main specs | Constitution plus the feature folder | All steering, every time, plus the spec | The bead | The issue and the contract | Org instructions plus the milestone |
| A one-line bug | A full plan, and TEST always runs in v1 | One change folder | A bug-fix process | A bugfix spec | One bead | One issue | One delegation |
| Where work starts | A sentence in the terminal | A slash command in the agent already in use | A slash command | The IDE | `bd` or the factory | An issue or one line | Slack, Jira, Linear |
| The artifact to show someone | The merge proposal | The archived change | The feature folder | The spec files | The bead history | The receipt and the not-proven list | The PR |

## Keep these

These are the reasons to use ATeam instead of OpenSpec or Beads. Later features should make them visible, not replace them.

- A record has a life: proposed, approved, rejected, superseded, retired. Rejected records stay, keyed by an evidence fingerprint, so discovery does not propose them again.
- Discovery separates "this is written down" from "this is what the code happens to do." An observation binds only after the owner says it is intended.
- The worker that builds a change does not grade it, and it cannot edit the contract or the approved records.
- A diff outside the contract's paths fails the attempt. Incidental paths (lockfiles, snapshots) are named on the contract.
- A pause is a named situation: a rule has to change, the work left the contract, the budget is gone, a check cannot be resolved, a sensitive action was about to happen, the environment failed, or records drifted.
- Predicted difficulty is stored next to tokens, retries, time, and the verification outcome.
- The brief is selected. A worker on one API receives the records bound to those paths. Global records are capped.

## Close these gaps, in order

People will try OpenSpec or a Beads-backed factory first, because those finish a change today. Finish the designed loop, then close the gaps in this order.

1. **First session attached to a real request.** Init proposes a handful of records for the next change and shows one plain example of what the next agent would have got wrong. A walkthrough of dozens of inferred records will be approved unread.
2. **One approval packet.** The request, each contract in a few lines, the acceptance checks, the new records, the approved records that overlap (the two statements side by side), the brief the worker will receive, the dollar budget, and the conditions that pause the run. Paths the plan will touch that have no approved record show up on the packet.
3. **The merge proposal is the definition of done.** Each criterion is met, not met, or cannot determine, with the test name or the evidence. Include the scope result, records proposed along the way, and cost against the budget. "Cannot determine" is an owner choice: accept the risk, or hold.
4. **A pause asks one question.** Scope exceeded: keep the extra file, revert it, or abandon. A rule has to change: the proposed replacement beside the current statement. Budget gone: raise it or stop. State what already succeeded.
5. **Status is the next human action.** One line: approve the plan, running, paused on a named decision, or approve the merge. Contract states stay available on an expanded status.
6. **A short lane for small fixes.** Skip investigation and use a light build when uncertainty is low. TEST still runs when the files are bound by an invariant or the risk is not low. Convention-only edits can stay light. This is a product change to the v1 rule "TEST always runs." Make it explicit when you change the spec.
7. **An outcome note at merge.** What changed for a user of the software, which contracts landed, which risks were accepted. Requirements stay free text until the same `serves` phrase shows up on many records.
8. **One calibration line on the next packet.** Example: the last three API contracts were marked medium and each took two extra attempts. A dashboard waits.
9. **Accepted risk comes back.** A risk the owner accepted is shown again the next time a plan touches those paths, until the owner clears it.
10. **Intake from an issue or a markdown file, and a status that pastes back** into the ticket. A tracker inside ATeam would rebuild Beads.

After those ten have been used on a real target repo, a read-only rendering of records and the open plan is worth a second person's time. Build it after the terminal packet is good.

Leave these until that use exists:

- A second model provider. The YAML in the target repo is already the portable part. The harness design keeps v1 on Claude.
- A solver that proves two records contradict. Side-by-side overlap at approval covers the case the owner will actually hit. Kiro's requirements analysis is the product to watch, not to clone in v1.
- Semantic search over records. Selection by path is the product.
- Deploy gates, team permissions, and SSO.
- Computer-use QA of a running UI. Factory's validation pass is a different product. Named checks plus a blind TEST pass are the wedge.
- Auto-merge, and any path where the builder edits its own acceptance criteria. OpenSpec allows artifacts to be revised during implementation. That convenience is the failure this product exists to close.

## One-line distinction

OpenSpec remembers what the system does. Beads remembers what to do next. Kiro and Spec Kit remember the plan for this feature. Factory and Devin do the work and hand back a pull request. ATeam remembers the rule that must stay true, keeps the worker from rewriting the finish line, and asks the owner twice: once before the work, once before it lands.

## Future runtime: Rust scanners, Rust memory, Go supervision

The harness design names Go and Rust as out of v1: Python only until a concrete need appears. The needs below are real, and they arrive after `ateam run` can finish a contract. Deploy them as separate binaries beside the Python CLI. One release tag builds all of them. `ateam doctor` prints each version and refuses a mismatch.

Python remains the only writer of `.ateam/` records and of `~/.ateam/<repo-id>/state.db`, and the only process that talks to the Claude Agent SDK. Rust and Go call Python (or are called by it) through argv and JSON. They do not open the state database as a second writer, and they do not call git on their own. Worktrees, record edits, and state transitions stay behind the Python commands that already own them.

### What each language is for

| Piece | Language | Owns | Stays out of |
|---|---|---|---|
| CLI the owner types, records, contracts, briefs, Claude sessions, state transitions | Python | Product rules and the model session | Long-running scan heaps, process-pool supervision once that pool is the measured problem |
| Deterministic bug and security scanners | Rust | One-shot analysis of a worktree or a diff, with a bounded memory budget | Approving records, judging acceptance criteria, calling a model |
| In-memory index for those scanners, when a one-shot process is too slow | Rust sidecar | The parse and automaton heap for one target repo | LLM context, the record graph (hundreds of YAML files stay in Python; a SQLite index is the step the spec already names) |
| Process supervisor for many workers | Go | The target lock, the worker cap, spawn, timeout, and cancel of Python worker processes | The Claude SDK, git, and the state machine |

Rust's memory model matters for the scanners and the index: one owner of the parse heap, queries that borrow it, and a hard cap that forces a streaming pass when the tree is large. That property holds because the heap lives in its own process. Embedding the scanner in CPython with PyO3 shares the interpreter heap and gives up the isolation. Start with a subprocess. Consider an in-process extension only after process startup shows up in the timing of a real check.

Rust does not manage the model's context window. Briefs, frozen contracts, handoffs, and the cap on global records are the memory system for the agent. A sidecar that summarizes transcripts would be a second, unapproved memory. Leave it out.

### How a Rust scanner deploys

`TargetConfig.checks` is already a name plus a shell command (`src/ateam/config.py`). A scanner is one of those commands. The verifier runs it in the contract worktree, after the tree check and beside unit tests and type checks. No new plugin API.

Command shape:

```text
ateam-scan secrets --root <worktree> --base <sha> --head <sha>
ateam-scan api --root <worktree> --base <sha> --head <sha>
```

Stdout is a JSON document. Exit 0 means no blocking finding. Exit 1 means blocking findings, and the attempt fails verification the same way a failing test does. Exit 2 means the scanner itself broke, which is an environment failure, not a bad build. Stderr is for humans. The JSON is what the merge proposal quotes.

A finding carries a stable rule id, severity, path, line range, and a short redacted preview. Token and key material is reduced to a known prefix and a short suffix before it is written to a report, a brief, or SQLite. The raw value stays out of the worker prompt.

Wire it per target:

```yaml
checks:
  - name: secrets
    cmd: ateam-scan secrets --root . --base "$ATEAM_BASE" --head HEAD
  - name: api
    cmd: ateam-scan api --root . --base "$ATEAM_BASE" --head HEAD
```

The coordinator supplies `ATEAM_BASE` as the contract's base commit. Each contract scans its own diff. The final integration step scans the whole integration branch. A contract may add a check the target config does not have. It may not delete a target-level security check.

First scanner families, all deterministic:

- **Secrets.** Known token prefixes (cloud keys, GitHub tokens, model-provider keys, private-key blocks) plus a high-entropy fallback. Allow a checked-in, reviewed ignore file for fixtures. A new ignore entry is a record proposal, approved by the owner, not a comment the builder adds in the same commit.
- **API surface.** New or changed routes, and whether they sit on a path an invariant binds (for example an admin route, or a route that accepts a token). The scanner reports the route. The TEST role and the owner decide whether the route breaks a record. The scanner does not invent the invariant.
- **Bug patterns that are syntactic.** The same shape as a linter: a rule id, a match, a file. Behavior bugs stay with the named tests and the TEST role.

Distribution: a static binary from the same release as the Python package (`ateam`, `ateam-scan`), arm64 and amd64 for macOS first, Linux when checks run in CI. `ateam doctor` checks that `ateam-scan` is on `PATH` and that its version matches. A missing binary fails `ateam doctor` and is an environment failure if a contract's checks name it. The scanner does not phone home. Findings land in the verification evidence and the merge proposal.

A scanner may emit a proposed record (an observation, inferred, with the finding as evidence). The record stays `proposed` until the owner approves it. That is the same discovery rule as the rest of the product.

### How the Rust index deploys, later

Build the one-shot CLI first. Add a sidecar when a measured scan of a real target is too slow or too heavy to repeat per contract.

`ateam run` starts `ateam-scan serve` once for that target. The socket lives at `~/.ateam/<repo-id>/scan.sock` and dies with the run. Python sends the same request the CLI accepts (root, base, head, which families) and reads the same JSON. One process owns the file cache, the secret automata, and any parse trees. A memory ceiling is a flag with a default. Past the ceiling, the process drops trees and scans in streaming passes. If the socket dies, Python restarts it once and, on a second death, marks the check an environment failure.

The record graph does not move into this process. If loading records ever matters, the spec already names a disposable SQLite index derived from the YAML.

### How Go supervision deploys, later

Introduce Go when a measured Python run shows one of these: a worker that survives `ateam stop`, a pool that leaks processes or file descriptors, or an idle coordinator whose memory is the problem. A wish for a second language is not that measurement.

The human still types `ateam`. `ateam run` starts `ateam-supervise` and passes the plan id and the target root. The Go process:

1. Takes the target-level lock the spec already requires (one running plan per target).
2. Asks Python which contracts are ready, up to `max_workers`.
3. Spawns one Python process per attempt: `ateam worker --attempt <id>`. That child builds the clean-room Claude session, runs the role, and writes a `WorkerResult` JSON to stdout.
4. Enforces the turn and dollar budget by killing the child process group when Python reports the cap, or when the wall-clock limit on the attempt hits.
5. On `ateam stop`, signals the process group, waits, then asks Python to mark the attempt interrupted and leave the worktree in place.
6. Asks Python to apply every state transition. Go does not write SQLite and does not run git.

Python remains able to run a single worker with no Go binary, for tests and for `max_workers: 1`. The Go supervisor is the pool, not a second product.

Ship `ateam-supervise` in the same tarball as `ateam` and `ateam-scan`. No network service. A team server with accounts and SSO is a different product (the Beads Team Server shape) and waits until a second person is actually in the loop.

### Order of arrival

| Stage | Ship | Start when |
|---|---|---|
| Now | The Python loop in the foundations plan and the harness spec: records, contracts, run, pause, merge proposal | Already underway |
| First extra binary | `ateam-scan` as a named check, secrets and API surface, diff-scoped | The verifier runs checks, and a real target has tokens or routes worth failing a contract over |
| Later | `ateam-scan serve` sidecar with a memory ceiling | A one-shot scan of that target is too slow or too heavy per contract, measured |
| Later still | `ateam-supervise` in Go | Stop, cancel, or idle memory of the Python pool fails on a real run |
| Unscheduled | Go or Rust calling the model, or either of them writing `.ateam/` or `state.db` | A concrete SDK limit that the Python runner cannot meet. None is known from the 2026-10-24 spike |

## Sources

Re-check before treating versions or star counts as current.

- Harness design and foundations plan in this repo, 2026-10-24.
- Stackness, "Spec-driven development tools for coding agents," 12 October 2026. OpenSpec, Spec Kit, Kiro, BMAD, loki-mode, spec-superflow, Spectra, Agentforce ADLC, including the gate table.
- OpenSpec docs: `docs/overview.md` and `docs/getting-started.md` in Fission-AI/OpenSpec (specs as current behavior, delta specs, archive).
- Kiro docs, Steering and the docs index, pages updated October 2026. EARS and property checks corroborated by secondary reviews the same year.
- Steve Yegge, Gas Town page; Gas City / DoltHub notes on Beads, mid to late October 2026.
- Factory, Missions and Delegations posts (plan approval, milestone validation, Slack / Jira / Linear intake).
- ETH Zurich and LogicStar, arXiv 2602.11988, revised 29 September 2026, on context files.
- This repo's `TargetConfig.checks`: a deterministic check is a name and a shell command.

## Working agreement for the next agent

Brief the owner from "What to hand back" and answer questions from the tables. When the owner asks for a feature, map it to a numbered gap or say it is outside this list. When the owner asks to start Rust or Go, name the stage in "Order of arrival" and the trigger. Edit the harness spec only when the owner decides a product change (the short lane is the one already flagged). Leave the foundations plan's task list alone unless the owner asks to implement it.
