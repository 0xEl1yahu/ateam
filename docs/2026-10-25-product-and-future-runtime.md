# ATeam: product position, competitors, and later runtime

Date: 2026-10-25. Status: product notes for the owner. This document does not change v1 behavior.

The harness design is the source of truth for what v1 does: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`. The short brief for a fresh agent window is `docs/2026-10-25-product-handoff.md`. This file is the full record of the product comparison and of how Rust scanners, a Rust scan process, and a Go supervisor deploy later.

## The product

ATeam is a control layer around a coding agent, pointed at any git repository. The owner approves what gets built and what gets merged. Between those two approvals the tool investigates, builds, and tests, and it stops when a rule, the scope, the budget, or the checks give out.

It exists because intent dies between agents. Agent A implements something under an architectural decision or a business rule. Agent B inherits the code without the reason, and removes the abstraction, hardcodes the dynamic thing, adds a silent fallback, fixes a local bug while breaking a wider rule, or reports done without meeting the acceptance criteria. ATeam answers that with records, frozen contracts, and a separate judge. It is an engineering system around agents, not a smarter agent.

Three mechanisms, one per failure:

| Failure | What the owner gets |
|---|---|
| Loss of why | Decision, invariant, and convention records in the target repo, selected for the files this task will touch, delivered to the worker before it edits |
| Loss of state | One worktree per task, one coordinator that owns shared state, an integration branch |
| False done | The checks named on the contract, plus an independent TEST role. Both read the frozen contract. The builder does not grade its own work |

A record is a small YAML file under `<target>/.ateam/records/`. Its kind is `decision`, `invariant`, or `convention`. It carries a statement, a rationale, evidence, an origin (`documented` or `inferred`), a basis (`requirement` or `observation`), a status (`proposed`, `approved`, `rejected`, `superseded`, `retired`), and the paths it binds. Workers receive approved records only. Something found in the code is an observation until the owner says it is intended. Rejected records are kept, keyed by their evidence, so discovery does not propose them again.

A contract is a frozen task under `<target>/.ateam/contracts/`. Plan approval freezes its paths, its acceptance criteria, and the hashes of the records it includes. A worker cannot edit that file, and a later edit fails validation. Acceptance criteria name a test where a test can decide, and name a judgment where only a reader can. The TEST role receives the contract, the diff, the check results, and the records. It does not receive the builder's transcript.

The owner is asked twice, and at a pause:

- Plan approval, before any implementation. Tiers (light, standard, careful) are visible and overridable.
- Merge approval, before anything reaches the target branch. Work lands on an integration branch first.
- A pause when a binding rule needs to change, the diff leaves the contract, the budget is exhausted, verification cannot be resolved, a sensitive action is required, the environment fails, or records drift mid-run.

Allocation is three marks, each low, medium, or high. Complexity picks how hard the build thinks. Risk picks how deep verification goes. Uncertainty picks whether an investigation runs before the build. Predictions are stored next to tokens, cost, retries, time, and the outcome, so the next plan can be read against history.

v1 is Claude only, through headless Claude Code and the Python Agent SDK. One plan runs per target repo. Requirements stay free text. Records bind paths by glob. A record with no paths is global, and globals are capped (default five) so a brief stays short.

## What is in the tree today

On 2026-10-25 the package has the record model and store, record lifecycle, the contract model and selection, target config, path layout, and a CLI that prints its version. `ateam init`, `ateam plan`, `ateam run`, status, pause, and merge are specified in the harness design and the foundations plan. They are not shipped. Competitor comparisons below are against that designed product. Say so when quoting them.

Deterministic checks already have a home in config: a check is a name and a shell command (`checks` on `.ateam/config.yaml`). That slot is how a future scanner attaches, without a plugin API.

## Three jobs the market bundles together

| Job | What the person feels | Who already does it |
|---|---|---|
| Remember the work | The agent picks up the task after a crash | Beads, Gas City, OpenSpec task lists |
| Remember the behavior | A spec describes how the system acts now | OpenSpec, Spec Kit, Kiro, BMAD |
| Remember the constraint | A later agent cannot quietly undo a decision | The gap ATeam is aimed at |

ATeam is the third job. The other two are neighboring products. Copying them wholesale rebuilds a spec toolkit or a task ledger and gives up the constraint.

Instruction files (`AGENTS.md`, `CLAUDE.md`, Kiro steering, a Spec Kit constitution) are a fourth, weaker habit: standing rules loaded on every turn. An ETH Zurich and LogicStar study (arXiv 2602.11988, revised 29 September 2026) found developer-written context files raised resolution about 2.4% on average, not a significant change, and added up to 19% inference cost. Model-written files lowered resolution slightly. ATeam's bet is narrower context: the records that bind the files in this contract, shown to the owner on the approval packet before the run.

## Similar applications

Figures are from public pages read on or about 12–25 October 2026. Re-check versions and star counts before quoting them as current.

### Spec tools

These put a written agreement in the repo before code, and a person can read it.

**OpenSpec** (Fission AI) is the one people install on a codebase that already exists. The loop is explore (optional), propose, apply, archive, driven as slash commands inside the agent the person already uses. A change folder holds `proposal.md`, delta specs (`ADDED`, `MODIFIED`, `REMOVED` requirements), an optional `design.md`, and `tasks.md`. Archive merges the deltas into `openspec/specs/`, organized by domain, which is the living description of how the system behaves now. It does not call a model itself. About 38 agents, including Claude Code, Codex, Cursor, Copilot, Gemini CLI, and OpenCode. On 12 October 2026: v1.14.1, about 72k GitHub stars, on the order of 760k npm downloads that week. The person reviews the proposal. `openspec validate` runs before code. `/opsx:verify` runs after and does not block archive. Artifacts can be revised while the agent is implementing. Thoughtworks has described the workflow as fluid and suited to existing systems. One published pair of runs (March 2026, one author, no bare-agent baseline) used about half the tokens of Spec Kit on the same task.

**GitHub Spec Kit** is the reference toolkit. A project constitution, then specify, clarify, plan, tasks, implement. Optional checklist and analyze steps sit before code. A converge step, added in 2026, repeats until the agent reports converged. Per feature it writes `spec.md`, `plan.md`, `tasks.md`, and often `research.md`, a data model, and contracts, on a numbered branch. About 41 agent integrations. Copilot is the default. On 12 October 2026: v1.1.3, about 141k stars. MIT licensed. Field notes (including Birgitta Böckeler on martinfowler.com, and Thoughtworks teams) describe the markdown volume as more process than a three-to-five-point change deserves. It fits a new feature that warrants a plan and a data model, or a team already on Copilot.

**Kiro** (Amazon) is a spec-driven IDE, with a CLI, web, and mobile on the same `.kiro/` configuration. A prompt becomes `requirements.md` in EARS notation, `design.md`, and `tasks.md`, and the person approves between those phases. Quick Spec can skip the approvals. EARS patterns are ubiquitous ("THE SYSTEM SHALL"), event-driven ("WHEN … THE SYSTEM SHALL"), state-driven, optional, and unwanted behavior. Bug fixes get a bugfix spec instead of a feature spec. Steering files (`product.md`, `tech.md`, `structure.md`, plus `AGENTS.md`) load in every session and carry product intent, stack, and structure across features. The IDE can derive property-based checks from EARS requirements and run them when asked. Hooks fire on file events, tool use, spec tasks, or prompt submit, and a failing shell hook can block a tool call. Custom agents and sub-agents exist. Proprietary, with a free credit tier and paid tiers up to about $200 a month. It fits someone willing to change editor to have phase approvals inside the product. Steering is context. A rule binds when a hook or a check fails the run.

**BMAD-METHOD** installs agile roles (analyst through QA) and writes a brief, a PRD, an architecture, then epics and stories before implementation. Gates include PRD validation, code review, adversarial and edge-case review, and a retrospective per epic. About 48 platforms. On 12 October 2026: v6.12.2, about 54k stars. The ceremony is the product. It fits a large greenfield effort where a design mistake is expensive.

**loki-mode** turns an issue, a spec, or one line into a pull request. A delivery contract and its checks are written from the task before code. Tests and an Ed25519-signed receipt come after. An unverified run opens a draft pull request with a "NOT PROVEN" list. Claude Code by default. Business Source License 1.1, free for internal use, Apache-2.0 from 2030. On 12 October 2026: v11.3.9, about 1.1k stars. It does not deploy.

**spec-superflow** uses OpenSpec-style change folders, one approval of the proposal and tasks before code, and one review of the git range afterward, recorded as `verified` or `accepted-risk`. About 19 agent platforms. Small, and quiet after 24 September 2026 (v2.0.1, about 850 stars).

**Spectra** is a desktop app (macOS and Windows) plus a CLI over the same kind of change: proposal, specs, design, tasks, then verify, review, a security audit, and a drift check. It began as an OpenSpec GUI. The app binary is closed. The npm launcher is MIT. v3.0.0 on 11 September 2026.

**Agentforce ADLC** builds Salesforce agents, not general application code. It is the rare tool in this set with a gate before deploy. Out of scope for ATeam.

A useful gate summary from the 12 October 2026 survey: most of these tools stop at a pull request. OpenSpec's verify is advisory. spec-superflow and loki-mode record a verification result before a change counts as done. Kiro puts phase approval and property checks around the work. None of them freeze a contract the builder cannot edit, then have a second role judge it without the builder's notes.

### Work ledgers and software factories

These remember tasks and run many agents. They do not remember rules.

**Beads** (Steve Yegge, now part of the Gas City ecosystem) is a work ledger for agents. A bead has a title, description, type, priority, status, and dependencies. The CLI is `bd`: create, `bd ready` for work whose blockers are closed, close. The graph is versioned and survives a crashed process, so a new session continues without re-reading the chat. On mid-October 2026: about 28k GitHub stars, nearly 2,000 forks, and more than 1.7 million release downloads in its first year. Beads Team Server is the commercial shared ledger, including token accounting. The memory is the queue of work, not the reason a rule exists.

**Gas Town** is the multi-agent workspace manager open-sourced on 1 January 2026. **Gas City** is the later orchestration SDK: sessions, mail, formulas, molecules (a graph of beads for a multi-step job), and the ability to run many agents with a record of what finished. Public descriptions use the same sentence as ATeam: orchestration, not a smarter prompt. The unit they orchestrate is the bead. A formula is choreography of tasks. An ATeam contract is a frozen definition of done plus the rules that bind the files.

**Factory (Droids and Missions)** is the commercial long-job product. The person describes the outcome and approves the plan. An orchestrator splits the job into milestones. Worker sessions implement. Handoffs go through git. Each milestone ends in a validation pass: tests, regression checks, and, where the app has a UI, a worker that clicks through flows. Delegations take work from Slack, Microsoft Teams, Linear, and Jira. Cloud sandboxes ("Environments") are prebuilt images of repos, tools, and dependencies. An Agent Readiness score rates how prepared a repo is for Droids. Missions are described as working best on repos at the higher readiness levels. Pricing in 2026 comparisons starts around $20 a month. The human still receives a pull request.

**The workers.** Claude Code, Cursor (editor plus cloud agents), Devin and Devin Desktop (Windsurf rebranded under Cognition in June 2026), OpenAI Codex, and the GitHub Copilot coding agent plan, edit, run commands, and open pull requests. They now ship subagents, worktrees, rules files, hooks, and cloud sessions. ATeam v1 sits on Claude and uses those capabilities. It does not replace the worker. Devin's extra is a cloud machine with a desktop and a browser, and an Agent Command Center for many sessions. Cursor's extra is the editor plus handing a task to a cloud agent. Copilot's extra is "assign an issue, receive a PR" inside GitHub. Those are intake and execution. They are not a binding record with an owner-only lifecycle.

### Review products

Sonar, CodeRabbit, and the same pattern judge a diff against general quality rules or an inferred architecture. They never see the contract this change was supposed to meet. A Sonar-style check is a good named command in `.ateam/config.yaml`. It is a different product from the TEST role, which judges the frozen acceptance criteria and does so without the builder's commentary.

### What each neighbor is for

| If the owner wants | The existing product |
|---|---|
| A living spec of current behavior, edited as deltas, on any agent | OpenSpec |
| A constitution and a heavy plan per feature, especially on Copilot | Spec Kit |
| Phase approvals and EARS criteria inside one IDE | Kiro |
| A full agile document chain and named roles | BMAD |
| A signed receipt and an explicit not-proven list on the PR | loki-mode |
| A task graph the next agent session can resume | Beads |
| Many agents running a long job, with UI clicking in validation | Factory Missions |
| The coding itself | Claude Code, and the other workers |

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

## What to keep

These are the reasons to use ATeam. Later features should make them visible in the approval packet and the merge proposal.

- A record has a life the worker cannot advance. Proposed, approved, rejected, superseded, retired. Approval, rejection, and supersession go through the CLI and commit only `.ateam/` paths. Supersession is one transaction with the replacement.
- Origin and basis are part of the product. Documented versus inferred, requirement versus observation. The approval prompt for an observation says, in plain words, that this describes what the code does, and asks whether it is intended.
- Discovery drops candidates whose evidence matches a rejected record. Global candidates are capped.
- The contract's content hash is stored at plan approval. A later change fails validation. Scope is the contract's paths plus named incidental files. A diff outside that set fails the attempt.
- TEST runs at a tier at least as capable as the BUILD it checks, and it does not see the builder's report or notes.
- Pause reasons are an enum the owner can act on: `decision_change_needed`, `scope_exceeded`, `budget_exhausted`, `verification_unresolvable`, `sensitive_action`, `environment_failure`, `records_drifted`.
- Failure class is separate from "try a bigger model." Implementation, requirements, environment, invariant violation. Only an implementation failure with evidence that reasoning was the limit may step up one tier, once. Requirements and invariant violations pause for the owner.
- Calibration is a row per attempt: prediction, model, tokens, cost, turns, time, outcome, failure class. Exportable later. Useful first as one line on the next approval.
- Context is selected. Path-bound records go to the worker that will touch those paths. The brief labels what is binding, what is the task, what is verified prior work, and what is an unverified note from the previous worker. TEST receives the first three only.

## Improvements and future features

People will try OpenSpec or a Beads-backed factory first, because those finish a change today. Finish the designed loop (init, plan, run, pause, merge proposal) before adding surface. Then close the gaps in this order. Each item is a product gap a neighbor already makes easy.

1. **Attach the first session to a real request.** `ateam init` as a tour of the whole repo will propose more records than anyone will read, and unread approval breaks the rule that inferred intent must not bind. The first run should sit on the next real request, propose a handful of records, separate "this is written down" from "this is what the code happens to do," and show one plain example of what the next agent would have got wrong. Unknowns are questions in the init report, not records.

2. **One approval packet.** This is the screen the owner actually reads. It contains the request, each contract in a few lines, the acceptance checks, the new records, the already-approved records whose paths overlap (the two statements side by side), the brief the worker will receive, the dollar budget, and the conditions that will pause the run. Paths the plan will touch that have no approved record are listed. Overlap called out in plain language is the contradiction check v1 needs. A formal solver can wait.

3. **The merge proposal is the definition of done.** Write `.ateam/reports/<plan-id>-merge.md` as the artifact a person can forward. Each acceptance criterion is met, not met, or cannot determine, with the test name or the cited evidence. Include the scope result, records proposed or changed, unresolved risks, and cost against the budget. "Cannot determine" is a choice: accept the risk, or hold the merge. loki-mode's not-proven list is the pattern. An accepted risk is recorded, not waved through in chat.

4. **A pause asks one question.** Stopping is the right behavior, and it has to read as a decision. Scope exceeded: the extra path, then keep it (expand the contract), revert it, or abandon. A rule has to change: the proposed replacement beside the current statement, approve or hold. Budget gone: raise it or stop. Environment failure: the command and the output. Say which contracts are already verified so the owner does not re-read the plan.

5. **Status is the next human action.** One line: approve the plan, running, paused on a named reason, or approve the merge. The contract state machine stays available when status is expanded. Beads feels responsive because `bd ready` answers "what is next." ATeam's equivalent is the owner's next action, not the worker's.

6. **A short lane for a small fix.** Kiro has bugfix specs. Spec Kit grew a bug process. Beads is one command. If every null-check is a full investigation plus a careful TEST pass, the edits that quietly break invariants will happen outside ATeam. The short lane skips investigation and uses a light build when uncertainty is low. TEST still runs when the touched paths are bound by an invariant, or when risk is not low. Convention-only edits can stay light. This changes the v1 call that TEST always runs. Change the harness spec in the same edit that adds the lane.

7. **An outcome note at merge.** OpenSpec leaves a current description of behavior. ATeam leaves the records that happened to be touched, and the plan. After several merges the owner will ask what the product does now. On merge, record what changed for a user of the software, which contracts landed, and which risks were accepted. Keep requirements as free text. Promote a `serves` phrase to something stronger only after the same phrase appears on many records. The design already treats `serves` as that seed.

8. **One calibration line on the next packet.** "The last three API contracts were marked medium and each took two extra attempts." The tier override then refers to history. `ateam telemetry` can print the table and a CSV. A dashboard waits until someone asks for one.

9. **Accepted risk comes back.** A risk accepted at merge is shown again the next time a plan touches those paths, until the owner clears it. spec-superflow records `accepted-risk` once. ATeam should attach it to the paths.

10. **Intake from where the request already lives.** A sentence in the terminal is enough for one owner. The next intake is a markdown file or a GitHub issue body. The output is a comment-sized status. Factory's Slack, Linear, and Jira delegations are the far end of this, and they can wait. A tracker inside ATeam would be Beads, built again.

After those ten have been used on a real target, a read-only view of records and the open plan is worth building for a second person. Spectra exists because a pile of markdown in a chat is unpleasant to review. Build the view after the terminal packet is good.

Leave these until that use exists:

- A second model provider. The YAML under `.ateam/` is the portable artifact. v1 stays on Claude, as the spec says.
- A solver that proves two records contradict. Side-by-side statements at approval cover the case the owner will hit. Watch Kiro's requirements analysis rather than cloning it.
- Semantic search or embeddings over records. The spec already defers this. Selection by path is the product. Search is how a missed binding stays hidden.
- Deploy gates, SSO, and a permissions framework. The spec chooses a state machine.
- Computer-use QA that clicks through a running UI. That is Factory's validation pass. Named checks plus a blind TEST role are ATeam's wedge.
- Auto-merge, and any path that lets the builder rewrite acceptance criteria during implementation. OpenSpec allows artifacts to be revised mid-flight. That convenience is the failure ATeam exists to close.
- Per-worktree databases and containers. Targets whose checks share a resource set `serialize_checks` and run those checks one at a time.

The two artifacts a person should remember are the approval packet and the merge proposal. Everything else is how those two stay honest.

## One-line distinction

OpenSpec remembers what the system does. Beads remembers what to do next. Kiro and Spec Kit remember the plan for this feature. Factory and Devin do the work and hand back a pull request. ATeam remembers the rule that must stay true, keeps the worker from rewriting the finish line, and asks the owner twice: once before the work, once before it lands.

## Later runtime

The harness design leaves Go and Rust out of v1: Python only, until a concrete need appears. The needs below are real. They start after a contract can run, pause, and reach a merge proposal. They deploy as extra binaries in the same release as the Python CLI. They do not replace it.

Three boundaries stay put:

- Python is the only writer of `<target>/.ateam/` and of `~/.ateam/<repo-id>/state.db`.
- Python is the only process that opens a Claude Agent SDK session. The 2026-10-24 spike pinned that session shape (clean room, permission callback, structured output).
- Git operations stay behind the Python git wrapper. Rust and Go ask Python to prepare a worktree or apply a transition. They do not grow a second git client or a second state machine.

One release tag builds every binary that exists at that stage. `ateam doctor` prints each version and refuses to run a plan when they disagree. Install is one tarball or one package: `ateam` on the path, and `ateam-scan` beside it. No network service. A team server with accounts is a different product, the Beads Team Server shape, and it waits until a second person is in the loop. macOS arm64 and amd64 first, Linux when the same checks run in CI.

### What each language owns

| Piece | Language | Owns | Leaves alone |
|---|---|---|---|
| The CLI the owner types, records, contracts, briefs, Claude sessions, state transitions | Python | Product rules and the model session | A long-lived scan heap, and process-pool supervision once that pool is the measured problem |
| Deterministic bug and security scanners | Rust | One-shot analysis of a worktree or a diff, under a memory ceiling | Approving records, judging acceptance criteria, calling a model |
| An in-memory index for those scanners | Rust, as a sidecar, and only after a one-shot scan is too slow | The parse and automaton heap for one target, for the length of `ateam run` | The model's context window, and the record graph |
| Supervision of many worker processes | Go | The target lock, the worker cap, spawn, timeout, and cancel | The Claude SDK, git, and the state machine |

### Rust scanners

Security and bug-pattern checks are deterministic. They belong in the check list, in the same slot as unit tests and type checks, and they run before the TEST role. The verifier runs each `checks` command in the contract worktree. A scanner is one of those commands. There is no plugin interface to invent.

```text
ateam-scan secrets --root <worktree> --base <sha> --head <sha>
ateam-scan api --root <worktree> --base <sha> --head <sha>
```

Stdout is one JSON document. Stderr is for a person reading a log. Exit 0 means no blocking finding. Exit 1 means blocking findings, and the attempt fails verification the same way a failing test does. Exit 2 means the scanner itself failed to run, which is an environment failure: retry without stepping the model up. The merge proposal quotes the JSON, not the exit code alone.

A finding has a stable rule id, a severity, a path, a line range, and a short preview. If the preview would contain a token, a key, or a connection string, store a known prefix and a short suffix. The raw secret never enters a brief, a worker prompt, a report committed to the repo, or SQLite.

Per-target config:

```yaml
checks:
  - name: secrets
    cmd: ateam-scan secrets --root . --base "$ATEAM_BASE" --head HEAD
  - name: api
    cmd: ateam-scan api --root . --base "$ATEAM_BASE" --head HEAD
```

The coordinator sets `ATEAM_BASE` to the contract's recorded base commit. Each contract scans the diff it introduced. The final integration step scans the integration branch as a whole, after it is brought up to date with the target branch. A contract may add a check. It may not remove a target-level secrets or API check.

First families:

- **Secrets.** Known prefixes for cloud keys, git-host tokens, model-provider keys, and private-key blocks, plus a high-entropy fallback. Fixtures are excluded through a reviewed ignore file. Adding an ignore entry is a record proposal (inferred, observation) for the owner. The builder does not silence a finding by editing the ignore file inside the same attempt. The tree check already fails edits under `.ateam/`. Keep the ignore file there, or treat an ignore-file edit outside the contract paths as a scope failure.
- **API surface.** Report routes added or changed in the diff, and whether the path is bound by an approved invariant (an admin surface, a token-accepting route, a public write). The scanner reports the route and the record id. TEST and the owner decide whether the route breaks the rule. The scanner does not invent invariants.
- **Syntactic bug patterns.** The same shape as a linter: rule id, match, file. Behavior bugs stay with the named tests and the TEST role.

A scanner may also emit a proposed record: origin inferred, basis observation, evidence pointing at the finding. Status stays `proposed` until `ateam approve`. That is the same discovery rule as the rest of the product.

Distribution is a static binary built in CI, shipped next to `ateam`. `ateam doctor` checks that `ateam-scan` is on `PATH` when any configured check names it, and that the version matches the Python package. A missing binary fails the doctor, and a run that needs it pauses with `environment_failure`. The binary does not phone home. Findings live in the verification evidence and the merge proposal.

Start as a subprocess. A one-shot process exit releases the scan heap, which is the memory property worth having. An in-process extension loaded into CPython (PyO3) shares the interpreter heap and is worth measuring only if process startup dominates the time of a real check.

These checks are integrity checks on the diff, in the same family as the post-run tree check. They are not a sandbox. A worker can still write a script the allow-list misses. The scanner then sees the resulting tree. The harness design already states that layering.

### Rust memory

"Memory" here is the process heap of the scanners. It is not the agent's context window.

The agent's memory is already specified, and it stays in Python and git:

- Binding context is approved records, selected by path, plus injection when a bound path is touched.
- Task context is the frozen contract.
- Working context is the session and the scratch directory, and it is labeled unreliable.
- Handoff context is coordinator facts (commits, diff, check results, hashes, session id, cost) plus worker notes that the brief marks as unverified.
- Global records are capped so the brief stays short.
- Auto-memory on the worker stays off, because it would be an unreviewed channel shared across worktrees.

A Rust heap does not make a context window larger, and a sidecar that summarizes transcripts would be a second memory with no approval step. Leave that out.

The record graph also stays in Python. It is hundreds of small YAML files and five queries (bindings for a path, supersession, cycles, dangling references, impact). If load time ever matters, the spec's next step is a disposable SQLite index derived from the YAML, not a Rust rewrite.

The scan heap moves into a long-lived Rust process only after a measured one-shot scan of a real target is too slow or too heavy to repeat on every contract. Until that measurement, every check is a process that starts, prints JSON, and exits.

When the measurement says so, `ateam run` starts `ateam-scan serve` for that target. The socket is `~/.ateam/<repo-id>/scan.sock` and is removed when the run ends. Python sends the same request the CLI accepts (root, base, head, which families) and reads the same JSON, so the merge proposal does not care which mode ran. One process owns the file cache, the secret automata, and any parse trees. A memory ceiling is a flag with a default. Past the ceiling the process drops trees and scans in streaming passes. If the socket dies, Python starts it once more. A second death is an environment failure on the check that was in flight.

That process boundary is the deployment of Rust's ownership model: one owner of the heap, callers that receive a finished report, and a ceiling the operating system can enforce. Sharing the heap with the Python interpreter gives the ceiling up.

### Go supervision

Go arrives when a measured Python run shows a concrete pool problem: a worker still alive after `ateam stop`, leaked processes or file descriptors, or an idle coordinator whose memory is the problem. Preference for a second language is not that measurement.

The owner still types `ateam`. For a target with one worker, and for tests, Python runs the attempt itself and no Go binary is required. When the pool is in use, `ateam run` starts `ateam-supervise` with the plan id and the target root.

The supervisor:

1. Takes the target-level lock the spec already requires, so a second plan can be approved and still cannot execute.
2. Asks Python which contracts are ready, up to `max_workers` from config.
3. Spawns one Python child per attempt: `ateam worker --attempt <id>`. The child builds the clean-room session, runs INVESTIGATE, BUILD, or TEST, and writes a `WorkerResult` JSON document to stdout. Budgets and turn caps inside the session stay in that child, because the SDK enforces them.
4. Kills the child process group when the wall-clock limit on the attempt hits, or when Python reports that the session has already ended on budget and the process has not exited.
5. On `ateam stop`, signals the group, waits, then asks Python to mark the attempt interrupted and leave the worktree where it is. Resume stays a Python decision: resume the Claude session when the contract hash, record hashes, and worktree HEAD match the checkpoint, otherwise start a fresh session from the worktree with the checkpoint in the brief.
6. Asks Python to apply every state transition. Go does not write `state.db` and does not run git.

Worker cap, dependency order, and "one plan per target" stay the scheduling policy from the spec. Go is the process supervisor that makes stop and timeout reliable. It is not a new scheduler with its own queue. Beads and Gas City already occupy "a new work queue."

`ateam-supervise` ships in the same tarball, version-locked by `ateam doctor`. It listens on no port. The owner does not configure it separately from `max_workers` and the attempt budget.

### Order of arrival

| Stage | What ships | Start when |
|---|---|---|
| Now | The Python loop: records, contracts, plan, run, pause, merge proposal, approval packet | Foundations are underway. The CLI is still version-only |
| First extra binary | `ateam-scan` for secrets and API surface, diff-scoped, wired as named checks | The verifier runs checks, and a real target has tokens or routes worth failing a contract over |
| Later | `ateam-scan serve`, with a memory ceiling and the same JSON | A one-shot scan of that target is too slow or too heavy per contract, measured |
| Later still | `ateam-supervise` | Stop, cancel, or idle memory of the Python pool fails on a real run |
| Unscheduled | Go or Rust calling the model, or either writing `.ateam/` or `state.db` | A concrete SDK limit the Python runner cannot meet. The 2026-10-24 spike did not find one |

Product work and runtime work stay in that order. A scanner attached before the merge proposal exists has nowhere to put a finding the owner will read. A Go supervisor attached before one Python worker can finish a contract supervises an empty pool.

## Sources

Re-check before treating versions or star counts as current.

- This repo: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`, `docs/superpowers/plans/2026-10-24-ateam-foundations.md`, and `src/ateam/config.py` (`NamedCheck` is a name and a shell command). Spike notes from 2026-10-24 are folded into the spec.
- Stackness, "Spec-driven development tools for coding agents," 12 October 2026. Counts, licenses, and the before-code / after-code / before-deploy gate table for OpenSpec, Spec Kit, Kiro, BMAD, loki-mode, spec-superflow, Spectra, and Agentforce ADLC.
- OpenSpec `docs/overview.md` and `docs/getting-started.md` (Fission-AI/OpenSpec): specs as current behavior, delta specs, propose / apply / archive.
- Kiro docs for steering and the product index, pages current in October 2026. EARS, property checks, and hooks corroborated by secondary reviews the same year.
- Gas Town (yegge.ai) and Gas City / DoltHub notes on Beads, mid to late October 2026, including the first-birthday download figures.
- Factory posts on Missions (plan approval, milestone validation, computer-use QA) and on Delegations (Slack, Teams, Linear, Jira).
- ETH Zurich and LogicStar, arXiv 2602.11988, revised 29 September 2026, on repository context files.
