# ATeam harness design

Date: 2026-10-24. Status: draft for owner approval.

## Intent

ATeam is an engineering system around coding agents, not a smarter agent. It is a standalone `ateam` command-line tool, pointed at a target git repository, that keeps engineering intent alive across agents, sessions and handoffs. The owner approves what gets built and what gets merged. ATeam decides how to build and verify it in between.

The problem it solves is continuity of intent. Agent A implements something under an architectural decision or business rule. Agent B inherits the code without the reason, and removes the abstraction, hardcodes the dynamic thing, adds a silent fallback, fixes a local bug while breaking a wider invariant, or reports done without meeting the acceptance criteria. ATeam prevents those failures with mechanisms, not with agents remembering conversations.

Three failure classes, each with its own mechanism:

| Failure | Mechanism |
|---|---|
| Loss of *why* | Decision records in the target repo, selected per task and delivered to the worker |
| Loss of *state* | One worktree per task, a coordinator that owns all shared state, an integration branch |
| False *done* | Deterministic checks plus an independent TEST role, both reading the frozen contract |

## Goals

- Durable, version-controlled engineering intent in the target repo: decisions, invariants, conventions, task contracts.
- Context selection: a replacement worker receives the decisions that bind the files it will touch, before it edits them.
- Decision provenance: every record says why it exists, what evidence supports it, which paths it binds, and what superseded it.
- Controlled revision: workers can propose changes to decisions; only the owner approves them.
- Independent verification: a task is integrated only when deterministic checks pass and a separate TEST worker confirms the acceptance criteria.
- Dynamic allocation: reasoning capacity from complexity, verification depth from risk, investigation from uncertainty.
- Role separation: a worker is BUILD, INVESTIGATE or TEST, never more than one.
- Interruptible: status can be inspected, execution stopped and resumed without losing progress.
- Calibration data: predictions and actuals are recorded for every task.

## Non-goals (v1)

- Providers other than Claude. No adapters, no multi-model routing, no abstraction for future LLMs.
- Go or Rust components. Python only until a concrete need appears.
- A graph database, vector store or distributed infrastructure.
- Requirements as first-class graph nodes (see Records: `serves`).
- Automatic documentation generation or a large bootstrap of records.
- A permissions framework. A small explicit state machine instead.
- Sophisticated resource scheduling. A global worker cap and dependency order are enough.
- Per-worktree provisioning of external resources (a database per worker, containers per contract). Targets whose checks share a resource serialise those checks instead.
- More than one running plan per target repo at a time.
- Driving interactive Claude Code sessions, background sessions (`--bg`), agent teams, or the Workflow tool. Workers are headless runs owned by the coordinator.

## Anti-goals (failure even if it technically works)

- **Workers redefining success.** A worker must never be able to change its contract, its acceptance criteria, or any approved record.
- **Self-approval.** The worker that built a change never judges it.
- **Inferred intent becoming binding without the owner.** Nothing discovered from code is a constraint until approved.
- **Observed behaviour mistaken for requirement.** A bug or workaround found in code must be labelled as observation, never silently promoted to invariant.
- **Contradictory instructions left behind.** Derived text (briefs, rules) is regenerated from approved records only; superseded records never reach a worker.
- **Trusting the worker's report over the worktree.** Integration verifies the actual tree state, including staged, unstaged and untracked files.
- **Silent escalation.** A failure is classified before any change in model or effort.
- **Silent merges.** Nothing reaches the target branch without the owner's approval.

## Constraints

- Claude Code 2.1.310 and the Python Agent SDK (`claude-agent-sdk`, 0.2.174 at time of writing) are the execution layer. ATeam uses what they do natively and builds only what they do not.
- The ATeam repo is `~/Documents/GitHub/ateam`. It is nested inside an unrelated parent git repository at `~/Documents/GitHub`. Never stage or alter the parent. Confirm `git rev-parse --show-toplevel` ends in `ateam` before any git command.
- Durable intent lives in the target repo under `.ateam/`. Operational state lives outside git.
- Workers run in isolated git worktrees. The coordinator creates and removes them with plain git; headless runs do not clean up after themselves.
- Workers run in a clean room so they inherit nothing from the target repo's own Claude configuration unless ATeam delivers it. The spike showed that Claude Code's `--bare` flag only authenticates with an API key, not a claude.ai login, so the clean room is built from the SDK options instead: no setting sources, strict MCP config, slash commands disabled, auto-memory disabled. Under that set, neither CLAUDE.md nor `.claude/rules/` nor any hook from the target reaches the worker. Existing CLAUDE.md and AGENTS.md files are still read by discovery as sources of engineering knowledge.
- Enforcement is layered: tool allow and deny lists, permission callbacks and hooks in Python, path deny rules, and a post-run diff and tree check. None of these is a complete security boundary and the design does not claim one.
- Nothing is built on an assumption about SDK, bare-mode, hook or path-scoped-rule behaviour until the spike (below) has shown it.

## Decisions made by the owner

- **Scope.** Own repo, own CLI, pointed at any target repository. Not tied to any one project.
- **Ecosystem.** Claude only in v1: headless Claude Code through the Python Agent SDK, subagent definitions, hooks, skills.
- **Coordinator.** Python owns orchestration, task state, worker lifecycle, context assembly and verification.
- **Two homes for truth.** Git for durable intent. A separate transactional store for runtime state (assignments, status, retries, leases). Operational state is never forced into git.
- **Two mandatory approval gates.** Plan approval before any implementation. Merge approval before anything reaches the target branch.
- **Autonomous between gates**, within approved boundaries: allocate workers, create worktrees, implement, test, investigate failures, retry within limits.
- **Pause conditions.** ATeam stops and asks when: a binding decision or invariant needs changing; work materially exceeds approved scope; retries or budgets are exhausted; a destructive operation, deployment or sensitive resource is needed; a critical verification failure cannot be resolved within the contract.
- **Integration branch.** Work integrates on an isolated branch, never directly on the target branch.
- **State machine, not permissions framework.** Simple and explicit.
- **Independent verification includes deterministic checks.** Tests, type checks, linting and integration checks are evidence; the TEST role is not the sole authority.
- **Task completion is not integration.** A finished BUILD is a candidate, nothing more.
- **Records are small, scoped, machine-readable**, with explicit ownership and references. No long project-memory documents.
- **Discovery is progressive and task-driven.** `ateam init` proposes a small baseline. Later plans propose records relevant to the components they touch. Five accurate records beat fifty speculative ones.
- **Discovery classifies** each proposal as explicitly documented or inferred, and as requirement or observation, with evidence. Nothing inferred binds without approval.
- **Requirements are free text in v1.** Promotable to nodes later.
- **Allocation uses three dimensions**: complexity, risk, uncertainty, each Low, Medium or High, from a fixed rubric, repository evidence and planner judgment. Complexity sets reasoning capacity. Risk sets verification depth. Uncertainty sets investigation. Shown per contract at plan approval, overridable by the owner.
- **No automatic escalation.** Failures are classified as implementation, requirements, environment or invariant violation. Model capability escalates only when there is evidence more reasoning would help.
- **Concurrency** is bounded by a global worker cap and task dependencies.
- **Calibration.** Predicted complexity, risk and uncertainty are recorded with actual token use, retries, execution time and verification outcome.
- **Tree verification.** The worktree's real state is checked, not the worker's reported commit.
- **Protected files.** Contracts and approved records are protected from worker modification.
- **Spike first.** SDK, bare mode, hooks and path-scoped rules are validated by a small executable spike before the integration is committed to.
- **Feature work happens in git worktrees**, for ATeam's own development as much as for its workers. Never on a branch in the main checkout. (Owner, 2026-10-25.) Carve-out, owner 2026-10-25: **read-only investigations run in the owner's checkout**, under enforced read-only permissions, with a before-and-after snapshot of the checkout (`git status --porcelain`, HEAD, stash list) that marks the attempt `tamper` if anything changed. A worktree for a role that cannot write costs a checkout, a branch and the loss of uncommitted context for no isolation gain. BUILD and TEST, which write, keep their worktrees.
- **Enforcement is pre-guard first, instructions last.** (Owner, 2026-10-25.) What a worker may do is decided before a tool runs: the tool list the session is launched with, then the permission callback and PreToolUse hook in Python. The role's system prompt is explanation, never the control. A post-run check backs each pre-guard: tree and scope for the worktree, the intent-repo snapshot for intent, the checkout snapshot for in-place investigations. Workers hand information back; the coordinator decides what intelligence and resources each step gets and what counts as validated.
- **Git-native mechanisms only.** Nothing ATeam depends on may live in clone-local state such as `.git/info/exclude` or local config; a fresh clone on another machine must behave identically. Ignore rules go in the target's tracked `.gitignore`. (Owner, 2026-10-25.)
- **Durable intent lives in a private intent repo, not in the target.** (Owner, 2026-10-25.) Target repos are often public; records and contracts are private engineering notes. The target carries nothing of ATeam's: no `.ateam/` directory, no ignore lines, no scratch files. Records, contracts, plans, reports and `config.yaml` live in their own git repository under ATeam's home, `~/.ateam/<repo-id>/intent/`, created by `ateam intent init`, optionally pushed to a private remote so another machine gets the same intent by pulling it. Approval commits land in the intent repo. The target branch is still where integration happens; the target itself carries no ATeam files. See "Intent repo" below. This supersedes the earlier "Two homes for truth" wording that placed `.ateam/` inside the target, and the "Target repo layout" section.
- **Workers never run `rm`.** Deletion is an integration-time action. A tracked file a worker must remove goes through `git rm`, so the deletion is in the reviewed diff. (Owner, 2026-10-25.)
- **Approval commits land on the target branch in the owner's checkout.** `ateam approve`, `ateam reject` and `ateam supersede` stage only `.ateam/` paths and commit on the configured target branch. They refuse to act if the checkout is on another branch or `.ateam/` already has uncommitted changes.
- **Investigations default to the careful tier.** `ateam init` and `ateam plan` run at `careful` unless `--tier` says otherwise; depth of reading is worth more than the dollar at planning time. (Owner, 2026-10-25.)
- **INVESTIGATE may `cd` inside the worktree.** The target must resolve to an existing directory under the worktree (the owner's checkout for in-place investigations); anything else, including `cd` alone and `cd -`, is denied. (Owner, 2026-10-25.) Extended on 2026-10-26 to BUILD and TEST, to absolute paths only, and to the worker's scratch directory: see the 2026-10-26 bullets below.
- **Plan, review, hand back.** Every `ateam plan` runs the planner, then a staff-engineer review of the draft by a fresh INVESTIGATE session; blocking findings earn the planner one revision round; the review travels with the plan to the owner. `--no-review` skips it. (Owner, 2026-10-25.) Since 2026-10-26 (owner): blocking or important findings earn the one revision round, and no second review runs after it.
- **Records for files that do not exist yet** are accepted when a contract in the same plan declares the path; they stay proposed like any other record. (Owner, 2026-10-25.)
- **Setup runs once per worktree.** `config.setup` commands run in a worktree before its first attempt, never again for that worktree; a failing setup stops the run. (Owner, 2026-10-25.)
- **Failure classification** is the planner's rule (a failed `test`/`command` criterion is `implementation`) plus a `requirements_unclear` flag the TEST worker may raise, which classifies the attempt `requirements`. (Owner, 2026-10-25.)
- **`ateam contracts refresh` stops on drift**: it leaves the draft in place and reports; it never rewrites a drifted contract. (Constrains a future feature; implemented with it.) (Owner, 2026-10-25.)
- **Rebuild derives only from the intent repo.** Doctor's rebuild recreates state that the intent repo determines and nothing else. (Constrains a future feature; implemented with it.) (Owner, 2026-10-25.)
- **Merge is fast-forward only by default, in the owner's checkout.** (Constrains a future feature; implemented with it.) (Owner, 2026-10-25.)
- **A failed integration pauses; a stale proposal is refreshed.** `integrating -> paused(reason)`: a merge conflict or a setup failure pauses with `environment_failure`, failed final checks with `verification_unresolvable`, so `paused -> abandoned` is reachable. `merge_proposed -> integrating`: when the target moved after the proposal, re-running `ateam integrate` merges the new target tip, re-checks and writes a fresh proposal. `integrate` refuses a paused feature; resuming belongs to the pause/resume work. (Owner, 2026-10-26.)
- **Integration defaults** (owner, 2026-10-26): merge reads the local target ref and never fetches; ATeam never sets a git identity; any status entry, untracked files included, makes the owner's checkout dirty for merge; the final check set is `config.checks` plus every contract's checks, de-duplicated; integrate merges the head verify recorded for each contract; the integration worktree and branch are left in place after merge or reject, for a later cleanup command.
- **A moved target is merged into the integration branch.** When the target branch has moved since `ateam/<plan-id>` was created, `ateam integrate` merges the current target tip into the integration branch (never rebases it, so tree-verified contract commits keep their hashes) and then runs the full deterministic check set, so `ateam merge` can still fast-forward. This settles "rebase or merge" in step 4 of "Verification and integration". (Owner, 2026-10-26.)
- **Worker reads are confined to the worktree and the worker's scratch directory** (decoded path arguments, the Read/Glob/Grep tools, recursive searches of an ancestor directory). (Owner, 2026-10-26.) Decided, not yet implemented: it is a feature with false-positive risk and gets its own plan; today only ATeam's home is protected from reads.
- **A request already covered by an existing plan is a `plan_covered` outcome, not a rejection.** `ateam plan --replaces P-xxxx` hides that plan from the planner and marks it `superseded` when the new plan is approved. (Owner, 2026-10-25.)
- **BUILD and TEST `cd` only to an absolute path inside the worktree**, like INVESTIGATE, so the home check can resolve relative paths from every directory a worker enters. A relative `cd`, `cd` alone, `cd -` and any target outside the worktree are denied for every role with one reason: "workers may cd only to an absolute path inside the worktree or its scratch directory". (Owner, 2026-10-26.) The worker's scratch directory, the one writable place outside the worktree, is also a `cd` target for every role, by absolute path (controller ruling, 2026-10-26).
- **Three INVESTIGATE allow rows from the lexer round stand**: an escaped `"\$(x)"`, an escaped `` "\`" `` inside double quotes, and a `$(...)` inside a comment are text, not substitutions. (Owner, 2026-10-26.)
- **Workers run under `/bin/bash`.** The policy's shell model is bash 3.2 to 5.3: the lexer reads every command both as bash 3.2-5.2 and as 5.3 and checks the commands found under either reading; the worker environment sets `SHELL` and `CLAUDE_CODE_SHELL` to `/bin/bash` so the Bash tool does not run the login shell (zsh on macOS). (Owner, 2026-10-26.)
- **An approved plan that has not started can be superseded.** `ateam plan --replaces` accepts an approved plan when none of its work has started; approving the replacement marks the old plan and its contracts `superseded`, a terminal contract state reachable from `draft` and `ready` only. "Started" fails closed: any contract of the plan with an attempt, a contract transition row, a checkpoint, a verify marker, or a status other than `draft`/`ready` in its file or the store. A plan's contracts are those its file lists plus those the store files under it. (Owner, 2026-10-26.)
- **`approve --force` never leaves a started approved plan live beside its replacement.** When the replaced plan is approved and has started, `approve` refuses with or without `--force`; `--force` still approves a replacement whose predecessor is rejected, covered or gone, superseding nothing. (Owner, 2026-10-26.)
- **An approved plan that has not started can be rejected.** `ateam reject` applies the same fail-closed "started" test as supersede; the plan becomes `plan_rejected` and its contracts `superseded`, in one intent commit and one store transaction. (Owner, 2026-10-26.)
- **Read-only sessions keep no handoff file; interrupted planning is recorded.** INVESTIGATE sessions (init, plan, review, revision, worker-run INVESTIGATE) are not asked to update `handoff.yaml` and their briefs carry no Handoff file section: they cannot write, and their structured report is the only record. A planning session interrupted before its plan is written makes the plan `plan_rejected` (`interrupted`); the next `ateam plan` rejects any other plan still at `investigating` whose planning process is gone (`interrupted (found stale)`), and leaves one whose process is alive. A plan the cleanup finds whose committed plan file already says `plan_proposed` or `plan_covered` is completed in the store to match the file instead. One `ateam plan` runs per target at a time: any intent commit by another ATeam command, or a new branch or worktree, while a planning or review session runs voids its report as tamper. (Owner, 2026-10-26.)
- **Searches never follow symbolic links.** For every role the policy denies directory walks that follow links (`grep -R`, `rg -L`/`--follow`, `find -L`/`-H`/`-follow`, `tree -l`, `ls -L`, `du -L`), so a link inside a checkout cannot lead a worker into ATeam's home. Plain `grep -r`, `rg` and `find` do not follow links and stay allowed. (Owner, 2026-10-26.)
- **ATeam's own checkout may carry a gitignored `intent` link** to its intent repo, so the owner can browse plans and contracts from the checkout. It is a local convenience only: one tracked `.gitignore` line, nothing depends on it, and it is never committed or pushed. This is the one carve-out from "the target carries nothing of ATeam's", for ATeam's own repo. (Owner, 2026-10-26.)
- **Contracts of dead plans are never frozen.** `ateam contracts freeze` refuses, with or without `--force`, a contract that is `superseded` or whose plan is `plan_rejected`, `plan_covered` or `superseded` in its file or the store. (Owner, 2026-10-26.)
- **`c=rm; $c x` and `$(echo rm) x` stay BUILD residuals**, backed by the post-run tree check: the command word is made at run time, which no pre-run lexer can see. They are pinned as strict expected failures in the policy tests. (Owner, 2026-10-26.)

## Calls made by the assistant (owner to check)

- **Operational store is SQLite from day one.** Python ships it, it is transactional, and interrupt and resume need atomic writes. It lives at `~/.ateam/<target-repo-id>/state.db`, never inside the target repo. Nothing in it is authoritative; it can be rebuilt from `.ateam/` plus the worktrees.
- **One record kind** with a `kind` field, rather than separate schemas for decisions, invariants and conventions.
- **Records bind paths by glob only.** No component registry in v1. A record with no paths is global.
- **Records are delivered by ATeam, not by Claude Code's rules.** The brief, carrying the selected records, goes in through the system prompt append. On top of that, a Python PreToolUse hook on Read, Edit and Write injects the records bound to a path at the moment the worker touches that path (verified in the spike, check E). Nothing is written into `.claude/` in the worktree and no worktree exclude mechanics are needed. The system prompt must state that text labelled "ATeam binding record" inside tool results is authoritative, because the spike showed the model otherwise treats injected text in a tool result with suspicion.
- **Scope breach definition.** A worker's diff touching any file outside its contract's declared paths is "materially exceeds scope" and fails verification, except for files the contract explicitly allows as incidental (lockfiles, generated snapshots).
- **TEST never drops below BUILD.** The TEST worker runs at a tier at least as capable as the BUILD worker it checks.
- **Retry budget per contract** defaults to two BUILD attempts after the first, then pause.
- **Worker reports are typed.** Every worker ends with a structured output validated against a JSON schema. Prose is never parsed.
- **Records are written only by ATeam.** Approval and rejection go through the CLI, which edits the YAML and commits. Hand edits are detected by the validator and reported.
- **IDs are sequential** (`R-0007`, `C-0012`, `P-0003`) and ATeam is the sole allocator, so there is no collision in v1.
- **Rejected records are kept**, with the owner's reason, keyed by an evidence fingerprint, so discovery never re-proposes them.
- **Global records are capped** at discovery time (default five) to keep every worker's brief short.
- **`claude` binary pinning.** ATeam records the Claude Code version it was spiked against and warns on a different version, because headless defaults are changing (bare mode is due to become the default for `-p`; permission mode defaults to manual, which denies prompts).
- **Two planning passes.** The implementation plan is written in two passes. Pass one covers the spike and the parts that do not depend on its outcome: record schema and validator, contract schema, state store, CLI skeleton. Pass two, written after the spike's findings are folded into this document, covers the runner, enforcement, verification and integration.
- **Integration is incremental.** The plan's integration branch `ateam/<plan-id>` is created from the target branch when execution starts. Each contract's worktree branches from the integration branch tip at the moment the contract becomes `ready`. When a contract is `verified`, it is merged onto the integration branch immediately. Dependents therefore see the code they depend on, "diff against base" means the worktree's recorded base commit, and a merge conflict only arises when two independent contracts touched the same lines. The final integration step is the full check run on the integration branch, after a merge or rebase with the current target branch.
- **Runtime environment is declared, not discovered.** `.ateam/config.yaml` carries `setup` commands run once per new worktree, `env` for workers and checks, and the deterministic `checks`. A failing setup command is an `environment` failure. Where checks share an external resource such as a database, config sets `serialize_checks: true` and deterministic checks and TEST workers run one at a time per target under a lock. BUILD workers still run concurrently.
- **One running plan per target.** `ateam run` takes a target-level lock. A second plan can be investigated and approved while one runs, but not executed. This removes cross-plan integration ordering and most record-drift cases from v1.
- **Target branch is configured.** `.ateam/config.yaml` names the target branch; `ateam init` defaults it to the repo's default branch. If the target branch moves during execution, nothing happens until final integration, where the integration branch is rebased or merged onto the new tip before the full check run.
- **The target's own `.claude/` never reaches workers.** With no setting sources loaded, the spike confirmed that the target's CLAUDE.md and `.claude/rules/` are not read, so no sparse checkout or removal is needed. Discovery reads the target's CLAUDE.md, AGENTS.md, rules, agents and skills as knowledge sources and may propose records from them. The diff and scope checks still ignore `.claude/**`.

Spike findings folded in (see `spikes/2026-10-24-sdk-bare-hooks/report*.md`):

- **Roles are option sets, not agent definitions.** Running a defined agent as the main session through `--agent` applied its prompt but broke structured output: the model wrote JSON into prose instead of calling the structured output tool. A role is therefore a system prompt append, a tool list, hooks and a permission callback, assembled by ATeam per run.
- **Permission mode `default` plus the SDK permission callback is the policy gate.** Under `acceptEdits` the callback was never reached for Bash. Under `default`, every call that is not pre-approved reaches the Python callback, which can deny with a message the model sees. PreToolUse hooks on Edit, Write and Bash add a second deny with a reason, verified for both a protected-file edit and a shell redirect into `.ateam/`.
- **Stop hook gate works.** A Python Stop hook returning `decision: block` with a reason keeps the session going until the condition holds; the `stop_hook_active` input field is the loop guard.
- **Structured output is reliable.** Every clean-room run returned `structured_output` matching the schema. The SDK raises `ResultError` after yielding the result message when the subtype is an error (`error_max_turns`, `error_max_budget_usd`); the runner must catch it and still read the result.
- **Resume after a turn cap works.** A session ended by `max_turns` resumed by session ID and finished the task, including writing the handoff file. Per-message usage is exposed on every assistant message; a PreCompact hook can be registered. No automated threshold is designed.
- **Concurrency works.** Two queries gathered in one process ran in parallel with distinct sessions.
- **The clean room is also a cost necessity.** One run that loaded the owner's user settings (MCP servers, plugins) exhausted a 0.60 dollar budget on a trivial task; clean-room runs cost fractions of a cent.
- **Result envelope** on 2.1.310 carries `subtype`, `is_error`, `num_turns`, `terminal_reason`, `stop_reason`, `session_id`, `duration_ms`, `total_cost_usd`, `usage`, `model_usage` (per model: tokens, cache, cost, context window), `permission_denials`, `errors`.
- **Nested launch.** The spike was run from inside a Claude Code session and stripped the inherited `CLAUDE*` environment variables before spawning workers. Whether leaving them in place causes problems was not tested; the runner builds a fresh environment regardless.

## Architecture

```
ateam (CLI)
  └── coordinator (Python)
        ├── records       load, validate, graph queries, generate derived text
        ├── discovery     init and per-plan proposals (uses INVESTIGATE workers)
        ├── planner       request -> contracts with dependencies, criteria, tiers
        ├── state         SQLite: features, contracts, attempts, leases, telemetry
        ├── scheduler     worker cap, dependency order, worktree lifecycle
        ├── runner        Agent SDK sessions: role, tools, hooks, schema, budget
        ├── verifier      deterministic checks, tree check, scope check, TEST role
        └── integrator    integration branch, re-verification, merge proposal
```

Native capabilities ATeam relies on and does not rebuild (verified against `claude --help` on 2.1.310 and the official docs on 2026-10-24; items marked *spike* must be confirmed by the spike):

- Headless runs with a JSON result envelope and `structured_output` validated against a schema, with retries on mismatch. Branch on the result `subtype`, not the exit code.
- Dollar budget and turn caps per run.
- Agent definitions with `tools`, `disallowedTools`, `model`, `effort`, `maxTurns`, `permissionMode`, `omitClaudeMd`, and per-agent hooks.
- Tool allow and deny lists, `--restricted` mode, and permission modes. Path deny rules for `Edit`.
- Clean room through SDK options: no setting sources, strict MCP config, slash commands disabled, auto-memory disabled. Verified: no CLAUDE.md, no rules, no target hooks reach the worker. (`--bare` is not usable with a claude.ai login.)
- Path-scoped rules (`.claude/rules/*.md` with `paths:`) load only when project settings are loaded. Verified, but not used: ATeam delivers bound records itself through a PreToolUse hook.
- Python hook callbacks in the SDK for PreToolUse (deny, allow, additionalContext), PostToolUse, Stop and SubagentStop (block with reason), UserPromptSubmit (additionalContext), PreCompact, PermissionRequest. There is no SessionStart hook in the SDK; the system prompt append covers that role. Verified.
- SDK permission callback (`can_use_tool`) under permission mode `default`. Verified.
- Session IDs, resume and fork for continuity.
- Per-model usage and cost in the result envelope.

ATeam's own machinery, with no native equivalent:

- Record schema, lifecycle, graph queries, derived text generation.
- Contract schema and freezing.
- Context selection and brief assembly.
- Feature and contract state machines, persisted.
- Worktree lifecycle and the worker process pool.
- Allocation rubric and tier mapping.
- Verification gate and integration.
- Discovery with classification and idempotence.
- Telemetry for calibration.

## Intent repo (supersedes "Target repo layout" below for the location of `.ateam/`)

```
~/.ateam/<repo-id>/
  intent/                 a git repository, created by `ateam intent init`, optional private remote
    config.yaml           target_branch, setup, env, checks, serialize_checks, protected_paths, max_workers, tiers
    records/              R-0001-<slug>.yaml ...
    contracts/            C-0001-<slug>.yaml ...
    plans/                P-0001-<slug>.yaml   (pass two)
    reports/              merge proposals      (pass two)
  state.db                operational state, never versioned, rebuildable
  scratch/<contract-id>/  the worker's handoff notes, outside the worktree
  worktrees/              (pass two scheduler)
~/.ateam/targets.json     which target path maps to which id and how it was derived; `doctor` warns and `intent init --adopt|--fresh` resolves a change
<target>/                 nothing of ATeam's
```

The repo id must be the same on every machine that clones the target, or the intent repo cannot follow the code. It is derived from the target's `origin` remote URL when one exists (normalised: scheme, user and `.git` suffix removed), and from the resolved path only for repos with no remote. `ateam doctor` prints both the id and what it was derived from.

Commands: `ateam intent init [--remote URL] [--adopt|--fresh]` (creates the repo, directories, default config and first commit; with a remote that already has content, adopts it; writes nothing into the target), `ateam intent path`, `ateam intent remote URL`, `ateam intent push`, `ateam intent pull`. Approval, reject, supersede, retire and freeze commit in the intent repo with the `ateam:` prefix and refuse when the intent repo is dirty. A worker attempt during which the intent repo's head or cleanliness changed is recorded as `tamper` and fails, whatever the worker reported: that is the integrity backstop for intent, since the worktree tree check cannot see it. The previous guard "checkout must be on the target branch" no longer applies to approvals; it still applies to integration.

## Target repo layout

Superseded on 2026-10-25 by "Intent repo" above: the target carries nothing of ATeam's. Worker scratch directories live at `~/.ateam/<repo-id>/scratch/<contract-id>/`, outside the worktree, so no ignore rule is needed in the target.

## Records

One YAML file per record in `.ateam/records/`.

```yaml
id: R-0007
kind: invariant              # decision | invariant | convention
status: approved             # proposed | approved | rejected | superseded | retired
origin: inferred             # documented | inferred
basis: requirement           # requirement | observation
statement: >
  Reading state is only ever set by the owner; nothing infers it.
rationale: >
  Inferred progress produced false confidence in an earlier version.
evidence:
  - {type: doc, ref: docs/specs/2026-10-24-shelf-design.md#anti-goals}
  - {type: test, ref: tests/test_reading_state.py::test_no_inference}
binds:
  paths: ["src/data/books.py", "src/api/books/**"]
  incidental: []             # paths a worker may touch without breaching scope
supersedes: []
serves: ["owner controls their own data"]
fingerprint: sha256-of-normalised-evidence
history:
  - {at: 2026-10-24T10:12Z, by: ateam-init, to: proposed, why: "found in spec anti-goals"}
  - {at: 2026-10-24T11:03Z, by: owner, to: approved}
```

Field rules:

- `origin: documented` requires at least one `evidence` entry of type `doc`, `claude-md`, `agents-md` or `commit-message`. Otherwise `inferred`.
- Discovery from code alone always produces `inferred` plus `observation`. The approval prompt states, in plain words, that this describes what the code does and asks whether it is intended. Approval may set `basis: requirement`.
- `serves` is a list of short reusable phrases. The validator reports identical phrases across records. This is the seed for requirement nodes later; v1 does nothing else with it.
- `history` is append-only.

Lifecycle:

- **proposed**: created by `ateam init`, by a plan's investigation, or from a worker's structured report that proposes a change. Not binding. Never delivered to workers.
- **approved**: by the owner through `ateam approve R-0007`. The CLI shows the record next to every approved record whose globs overlap, so a potential contradiction is visible at the moment it could enter. Optionally an INVESTIGATE worker compares the proposal with the overlapping records and flags conflicts; the owner decides.
- **rejected**: kept with the owner's reason. Discovery checks fingerprints and never re-proposes a rejected record.
- **superseded**: only as one transaction with approving a replacement. The new record lists the old in `supersedes`; the old record's status flips; both commit together; derived text is regenerated. The validator refuses a state where a superseded record is still approved or a `supersedes` target is missing.
- **retired**: supersession with no replacement.

Graph: `binds`, `supersedes` and contract references are the edges. At runtime the records are loaded into an in-memory graph (networkx or plain dictionaries, builder's choice) with five queries: records binding a path, supersession chain, acyclicity, dangling references, and impact (every approved contract and path that references a record). No graph store in v1. If load time ever matters, a SQLite index derived from the YAML is the next step and stays disposable.

Validator (runs on every CLI command that reads records): schema, unique IDs, status consistency, dangling references, acyclic supersession, every glob matches at least one file (dead globs are reported, not fatal), duplicate `serves` phrases, hand edits (file hash differs from the hash recorded in state).

## Contracts

One YAML file per contract in `.ateam/contracts/`. Frozen at plan approval; a frozen contract's content hash is stored in state, and any later change fails validation.

```yaml
id: C-0012
plan: P-0003
title: Expose reading state through the books API
status: ready                # draft | ready | building | verifying | verified | failed | paused | integrated
depends_on: ["C-0011"]
paths: ["src/api/books/**", "tests/api/test_books.py"]
incidental: ["requirements.txt"]
records:                     # frozen selection with content hashes
  - {id: R-0007, hash: "..."}
  - {id: R-0002, hash: "..."}
acceptance:
  - id: A1
    statement: GET /books/{id} returns reading_state for every book
    check: {type: test, ref: tests/api/test_books.py::test_reading_state_present}
  - id: A2
    statement: PATCH with an invalid state returns 422 and changes nothing
    check: {type: test, ref: tests/api/test_books.py::test_invalid_state}
  - id: A3
    statement: No code path infers reading state (R-0007)
    check: {type: judgment}  # TEST worker decides, with evidence
checks:                      # deterministic, from .ateam/config.yaml plus contract additions
  - {name: unit, cmd: "python -m unittest discover -s tests"}
  - {name: types, cmd: "mypy src"}
allocation:
  complexity: medium
  risk: high
  uncertainty: low
  reasoning: >
    Touches an API boundary bound by R-0007; existing tests cover the model
    layer but not the API; requirements are explicit in the plan.
  tier: {build: standard, test: careful, investigate: none}
budget: {attempts: 3, usd_per_attempt: 2.00, max_turns: 60}
```

Workers receive contracts read-only. The contract's `records` list is what the brief contains.

**A contract is binding or work does not start.** No worker runs unless the contract was frozen through `ateam contracts freeze`, its content hash still matches the one stored at freeze, its status is runnable (`ready`, `building`, `failed` or `paused`), and none of its frozen records has changed, been superseded or gone missing. The runner enforces this before an attempt is recorded (`ateam.runner.binding.require_binding`), and the pass-two scheduler inherits the same check. Re-freezing after a deliberate change is explicit (`--force`). Acceptance criteria with a `check` of type `test` must be satisfied by that named test passing; criteria of type `judgment` are decided by the TEST worker with cited evidence.

## State machines

Feature (one per plan):

```
requested -> investigating -> plan_proposed -> plan_approved -> executing
executing -> integrating -> merge_proposed -> merged
executing -> paused(reason) -> executing | abandoned
integrating -> paused(reason)      (a merge conflict, a setup failure or failed final checks)
merge_proposed -> integrating      (the target moved after the proposal; integrate refreshes it)
plan_proposed -> plan_rejected
investigating -> plan_covered
investigating -> plan_rejected   (an interrupted planning session, or an unusable report)
plan_proposed -> superseded
plan_approved -> superseded        (only while none of its contracts has started)
plan_approved -> plan_rejected     (`ateam reject`, only while none of its contracts has started; its contracts are superseded)
```

`plan_covered` and `superseded` are terminal. `plan_covered`: the planner returned no contracts and named the plans that already cover the request. `superseded`: a later plan was approved with `--replaces` naming this one. An approved plan is superseded only when none of its work has started: every contract is `draft` or `ready` in its file and in the store, and none has an attempt, a contract transition, a checkpoint or a verify marker. Its contracts are superseded with it, in the same approval commit and one store transaction.

Pause reasons are an enum: `decision_change_needed`, `scope_exceeded`, `budget_exhausted`, `verification_unresolvable`, `sensitive_action`, `environment_failure`, `records_drifted`.

Contract: `draft -> ready -> building -> verifying -> verified -> integrated`, with `failed` (retry possible) and `paused` as side exits, and `superseded` (terminal, from `draft` or `ready` only) when its approved plan is superseded. A contract becomes `ready` when every dependency is `verified`.

Every transition is a row in SQLite with timestamp, actor and reason. `ateam status` reads only this table plus the live worker list. `ateam stop` sends interrupt to running sessions, marks their attempts `interrupted`, and leaves worktrees in place. `ateam resume` re-enters `executing` and either resumes the interrupted session by ID or starts a fresh attempt from the worktree, builder's choice per failure class.

## Roles

| Role | May | May not | Ends with |
|---|---|---|---|
| INVESTIGATE | read, search, run read-only commands from a strict allow-list (git log/show/diff/blame/status/ls-files/grep/branch --list, ls, cat, head, tail, wc, grep, rg, find, tree, du, stat, file, pwd, sort, uniq, cut; no interpreters, no sed, no awk) | edit, write, run anything that can write, run tests | findings report with evidence and proposed records |
| BUILD | read, edit, write, run checks, commit in its worktree | edit `.ateam/**` or `.claude/**`, touch files outside `paths` plus `incidental`, push, merge, run `rm` in any form (tracked files go through `git rm`; cleanup is an integration step), cd outside the worktree and its scratch directory, or by a relative path | build report: commits, files, checks run, risks, record proposals |
| TEST | read, run checks and tests, write only under a scratch directory | edit source, edit tests that the contract names, see the builder's transcript, cd outside the worktree and its scratch directory, or by a relative path | verdict per acceptance criterion with evidence |

Enforcement, in layers. Layers 1 to 3 run before a tool executes (pre-guards); layer 4 verifies afterwards. The system prompt is not a layer.

1. Session tool list: `tools` per role. INVESTIGATE sessions are launched without any editing tool.
2. SDK permission callback: deny any file write outside the contract's paths, any edit under `.ateam/` or `.claude/`, and any Bash matching a configurable destructive list (`git push`, `git merge`, `rm -rf`, deploy commands).
3. Path deny rules for `Edit` on `.ateam/**` and `.claude/**` in the session settings.
4. Post-run checks: for BUILD and TEST, the worktree must be clean except for committed changes (staged, unstaged and untracked files are reported and fail the attempt) and the diff against the base must touch only `paths` plus `incidental`; for every role, the intent repo's head and cleanliness must be unchanged; for in-place investigations, the owner's checkout status, HEAD and stash list must be unchanged. Any change is recorded as `tamper`.

Layers 1 to 3 stop built-in tools and recognised shell commands. They do not stop a script the worker writes and runs. Layer 4 catches the result. This is integrity verification, not a security boundary: the pre-guard exists so a confused or careless session cannot do damage, not to defeat an adversary (owner, 2026-10-26). Grading rule for policy findings: a path to a forbidden action through ordinary shell usage is Important; a contrived encoding is Minor, closed when the fix is a few lines and otherwise listed as a residual.

The TEST worker receives the contract, the diff, the deterministic check results and the records. It does not receive the BUILD worker's report, so it cannot be anchored by the builder's claims.

## Allocation

Three dimensions, each Low, Medium or High. The rubric lives in `ateam/rubric.md` and is short enough to read in a minute. Signals the planner must cite: component boundaries crossed, approved records bound, test coverage of the touched paths, migrations, concurrency, security surface, ambiguity in the request. File count alone is not a signal.

- **Complexity** picks the BUILD tier (model, effort, turn cap, budget).
- **Risk** picks verification depth: which checks are mandatory, whether a TEST worker runs at all for trivial changes (my call: it always runs in v1), whether an extra INVESTIGATE review of the diff is added, and whether the contract is flagged for the owner's attention at merge approval.
- **Uncertainty** decides whether an INVESTIGATE worker runs before BUILD and what questions it must answer. High uncertainty with unanswered questions after investigation pauses the plan for the owner.

Tiers are named in `ateam/config/tiers.yaml` and mapped per role to a Claude model alias, effort, max turns and budget. Three tiers in v1: `light`, `standard`, `careful`. The owner can override any tier per contract at plan approval. Model aliases are the builder's choice at build time from the current Claude Code aliases; the design does not pin them.

Failure classification runs after every failed attempt, by the coordinator from the structured report and check output, with an INVESTIGATE worker when the cause is unclear: `implementation`, `requirements`, `environment`, `invariant_violation`. Only `implementation` with evidence that reasoning was the limit may escalate one tier, once. `requirements` and `invariant_violation` pause for the owner. `environment` retries without escalation.

Telemetry row per attempt: contract, role, tier, predicted dimensions, model, tokens in and out, cost, turns, wall time, outcome, failure class. Stored in SQLite and exportable as CSV.

## Verification and integration

Per contract, in order:

1. Tree check (layer 4 above).
2. Deterministic checks from the contract, in the worktree. Any failure ends the attempt as `failed(implementation)` unless the check itself errored, which is `environment`.
3. TEST worker. Verdict per criterion: `met`, `not_met`, `cannot_determine`, each with evidence. Any `not_met` fails the attempt. `cannot_determine` on a judgment criterion pauses for the owner if it survives a retry.
4. Contract becomes `verified`.

Integration, incremental per contract and final per plan:

1. When execution starts, create `ateam/<plan-id>` from the configured target branch.
2. Each contract's worktree is created from the integration branch tip when the contract becomes `ready`, and that commit is recorded as the contract's base.
3. When a contract is `verified`, merge its branch onto the integration branch at once. If the integration branch moved since the contract's base and the merge conflicts, that is an `environment` failure for this contract: one BUILD attempt rebases it with the conflict as its only task, then it is re-verified.
4. When every contract is `integrated`, merge the current target branch tip into the integration branch if the target moved (owner, 2026-10-26: never rebase), then run the full deterministic check set on it. A failure here pauses with `verification_unresolvable` and the evidence.
5. Write the merge proposal to `.ateam/reports/<plan-id>-merge.md`: diff summary, evidence per contract, records proposed or changed, unresolved risks, telemetry summary.
6. `merge_proposed`. The owner runs `ateam merge P-0003` to fast-forward or merge into the target branch, or `ateam reject`.

## Context lifecycle, handoffs and recovery

Principle: a Claude session is temporary working memory. ATeam's records and contracts are durable engineering memory. Nothing in the system depends on a particular session surviving.

Four kinds of context, treated differently:

| Kind | Where it lives | Authority | Reaches a worker as |
|---|---|---|---|
| Binding | approved records | authoritative | brief section "Binding", plus PreToolUse injection when a bound path is touched |
| Task | frozen contract | authoritative for the task | brief section "Task" |
| Working | the session, the worktree scratch directory | unreliable | never carried between sessions except through the handoff file |
| Handoff | checkpoint per attempt | facts verified by the coordinator; notes unverified | brief sections "Verified prior work" and "Previous worker's notes" |

Checkpoint per attempt, two layers:

1. **Coordinator-derived facts**, always available even after a crash: worktree base commit and HEAD, list of commits, diff summary, deterministic check results so far, contract hash, record hashes, Claude session ID, tokens and cost. Written to SQLite at every state transition and at attempt end.
2. **Worker-authored notes**: a small typed `handoff.yaml` in the worktree's scratch directory (`.ateam-scratch/`, ignored through the target's tracked `.gitignore`, never through clone-local exclude files, so every clone behaves the same; `ateam init` adds the line) that the brief instructs the worker to update at milestones: progress, unresolved issues, files of interest, hypotheses, next actions. The schema is short. The coordinator reads it after the session ends, whatever the reason.

Brief assembly labels its sections by authority. The TEST role receives Binding, Task and Verified prior work only; it never sees a previous worker's notes or report.

Resume policy for an interrupted or budget-stopped attempt: native session resume is used when the contract hash, record hashes and worktree HEAD all match the checkpoint; otherwise a fresh session starts from the worktree with the checkpoint in its brief. Compaction inside a session is left to Claude Code; ATeam bounds sessions with turn and budget caps rather than managing context pressure.

Downstream revalidation: after a verified contract merges onto the integration branch, its deterministic checks run again on the integration branch. A failure there is an `environment` failure for that contract and blocks dependents from becoming `ready` until resolved.

Deferred until real failures are observed: token-budget allocation, context-pressure detection and session rotation, semantic retrieval or embeddings, summarisation hierarchies, live context sync between concurrent workers, custom compression. Auto-memory stays off for workers: it is shared across all worktrees of a repo and has no approval step, so it would be an unreviewed back-channel.

## Observability and provenance (pass two)

Owner decision, 2026-10-25: every worker attempt leaves an event log that is the primary record of what happened, so an error, a mistake, a lesson or a memory can be traced to the specific event that produced it. tmux is an optional viewer over those logs, never the execution substrate: workers are headless SDK sessions and are not steered through a terminal.

- **Event log per attempt.** `~/.ateam/<repo-id>/logs/<attempt-id>.jsonl`, append-only, one JSON object per line with `seq`, `at`, `attempt`, `contract`, `role`, `type` and a typed payload. Event types: `session_start`, `assistant_text`, `tool_call`, `tool_result` (truncated, with a hash of the full content), `policy_denial`, `record_injected`, `check_run`, `state_transition`, `pause`, `report`, `session_end`, `error`. The coordinator writes the log as the SDK stream arrives, so a crash mid-attempt still leaves everything up to the crash.
- **Event references.** An event is addressed as `A-<attempt-id>#<seq>`. `evidence` entries on records gain the type `event`, so a record proposed from a failure cites the exact denial, check or report that motivated it. Telemetry rows, checkpoints and pause reasons carry the `seq` of the event that caused them.
- **Lessons are evidence-based.** A line added to `CLAUDE.md`, a new record, or a change to the policy that follows a mistake cites its event reference in the commit message and, for records, in `evidence`. "We saw this go wrong" becomes "A-0042#17: PreToolUse denied `git push` after the worker reported done". The pass-two `ateam lessons` command lists proposed records and policy changes with their events.
- **Viewer.** `ateam watch [P-0001]` opens a tmux session with one window per running attempt tailing its log, a status window, and a window for pauses awaiting the owner. Closing it changes nothing. `ateam log A-0042 [--type policy_denial] [--since seq]` prints events without tmux, for SSH or after the fact.
- **Retention and privacy.** Logs live in ATeam's home next to the state store, not in the intent repo, because tool results contain code and secrets may pass through them. They are never pushed. Retention is a config value with a default measured in days; events referenced by a record are kept regardless.

## Doctor and medic (pass two)

Owner decision, 2026-10-25. `ateam doctor` is the harness's INVESTIGATE role turned on itself; the medic is its BUILD role. The thing that diagnoses never applies its own fix without a gate, and the split is in authority, not in commands: one `doctor` command with modes, two modules underneath.

- **Findings registry.** Each check is registered once with its severity, the evidence it produces (a path, an event reference `A-<attempt>#<seq>`, a hash pair), its class and, for the auto class, its fix function. A check with no fix registered is propose-only by construction. Neither doctor nor medic uses a model in v1; both are deterministic, which is what lets the medic be trusted with the auto class.
- **Classes.**
  - *auto*: ATeam's own operational state, which the design already declares a rebuildable cache. A run lock held by a dead process, an attempt left open by a crash or Ctrl-C, a worktree registration whose directory is gone, a schema one version behind, a missing scratch or log directory. Every one has a correct value recomputable from the intent repo and the worktrees. The medic may apply these without asking.
  - *propose*: durable intent. Record drift, a hand-edited contract, a superseded record still bound, an intent repo behind or diverged from its remote, a derivation change (`targets.json` disagrees). The fixes are approvals and go through the gates the owner already holds: re-freeze, supersede, pull, `intent init --adopt`. The medic writes proposals, never approves.
  - *human*: environment and credentials. Claude Code version drift from the spiked version, not logged in, missing Python or git. The doctor instructs; nothing acts.
  - The target repository is in no class: ATeam never writes there.
- **Modes.** `ateam doctor` diagnoses and prints the classified list (exit 1 when any finding is not auto-fixable). `ateam doctor --fix` applies the auto class only, re-running each check immediately before its fix so it never acts on a stale diagnosis, and reports what it changed. `ateam doctor --propose` writes the propose-class findings as proposed records or pending actions. `ateam doctor --rebuild` reconstructs the state store from the intent repo and the worktrees.
- **Documentation and lessons.** Every medic action is an event in the doctor's own log (`A-doctor-<n>#<seq>`) and a line in a doctor report under ATeam's home. A finding that recurs is input for `ateam lessons`: evidence-based, tagged to events, surfaced as a proposed record or policy change for the owner. A repair becomes a lesson, not a habit.
- **Human in the loop** is everything irreversible, everything that changes truth beyond closing orphaned operational rows, everything in the target, and everything with cost. Not in the loop: idempotent repairs of ATeam's own state whose correct value can be recomputed.
- **Why one command.** Two commands would cost a two-step flow and two code paths that must agree on every finding type, where a fix drifting from its check is the classic failure. One command with modes keeps the separation where it matters.

## Discovery

`ateam init <target>`:

1. Read docs, CLAUDE.md, AGENTS.md, README, configuration, tests, and recent git history. This is an INVESTIGATE worker with a fixed brief and a structured output schema of candidate records.
2. Classify each candidate (`origin`, `basis`), attach evidence, bind the narrowest glob the evidence supports, compute the fingerprint.
3. Drop candidates whose fingerprint matches a rejected record. Cap global candidates.
4. Write candidates as `proposed` and print an approval walk-through. Unknowns (things the worker could not determine) are listed in the init report, not stored as records.

`ateam plan <request>`: the same, scoped to the components the plan touches, producing both the contracts and any new proposed records. Plan approval and record approval happen in the same walk-through.

## CLI surface

```
ateam init <path>                 discover and propose a baseline
ateam plan <path> "<request>" [--tier careful] [--replaces P-xxxx] [--no-review]
                                  investigate, review and propose contracts
ateam approve P-0003 [--tier C-0012=careful] [--reject-record R-0009 "reason"]
ateam run P-0003                  execute until a gate or pause
ateam status [P-0003]
ateam stop P-0003 / ateam resume P-0003
ateam records [--path src/api/**] [--impact R-0007]
ateam approve R-0012 / ateam reject R-0012 "reason" / ateam supersede R-0007 --with R-0012
ateam merge P-0003 / ateam reject P-0003 "reason"
ateam telemetry [--csv]
ateam doctor                      claude version, SDK version, target repo health
```

`ateam init` and `ateam plan` both default to `--tier careful`.

## The spike (done 2026-10-24, throwaway)

Completed. Scripts and reports are in `spikes/2026-10-24-sdk-bare-hooks/`; findings are folded into "Calls made by the assistant" above. The original brief, for the record: a script in the ATeam repo under `spikes/` must demonstrate, against a scratch git repo, with the output captured as a markdown report:

1. An SDK `query()` with `cwd` set to a worktree, bare mode on, a role agent definition with restricted tools, and a JSON schema, returns `structured_output`.
2. Whether a path-scoped rule in the worktree's `.claude/rules/` reaches the worker under bare mode. If not, that a SessionStart hook's `additionalContext` does.
3. A Python PreToolUse hook or `can_use_tool` callback denies an `Edit` under `.ateam/` and the worker reports the denial rather than retrying through Bash.
4. A Stop hook can block the session ending until a named condition holds, and the Python return shape for that.
5. A Bash write outside the declared paths is caught by the post-run tree check even when layers 1 to 3 miss it.
6. The result envelope fields actually present (`subtype`, `structured_output`, cost, usage, `num_turns`, `is_error`) on 2.1.310.
7. Two concurrent `query()` calls from one process complete independently.
8. What the SDK exposes about context usage and compaction during a run: per-message usage, whether PreCompact and PostCompact hook events fire in headless mode and what they carry, and whether a session ended by turn cap or budget can still be resumed. No automated threshold is designed until this is known.

The spike's findings update this document's "Calls made by the assistant" section before the second planning pass is written (see "Two planning passes" above).

## Failure modes and mitigations

- **Records rot.** Globs die on refactor; statements go stale. Dead globs are reported at every plan. Each plan re-shows the records it selected for approval. Records bind narrowly.
- **Workers skim long briefs.** Global records are capped; path-scoped rules repeat the record at the point of use.
- **Semantic contradiction between records.** Overlap display at approval, optional INVESTIGATE comparison, owner as last check.
- **Mid-run supersession.** Frozen hashes in the contract; drift pauses with `records_drifted`.
- **Worker escapes enforcement with a script.** Tree and scope check fails the attempt. Not a security boundary; stated as such.
- **Headless defaults change between Claude Code versions.** Explicit flags for bare mode and permission mode; `ateam doctor` warns on version drift.
- **SQLite state and git disagree after a crash.** State is a cache; `ateam doctor --rebuild` reconstructs it from `.ateam/` and the worktrees.
- **Cost runaway.** Per-attempt and per-plan budgets; `budget_exhausted` pauses.

## What's left to the builder

- Python packaging, CLI framework, test layout for the ATeam repo itself.
- Exact JSON schemas for each role's report and for discovery candidates.
- The rubric text, kept to one page.
- Tier to model alias mapping at build time.
- Brief template wording per role.
- Whether to use networkx or plain dictionaries for the record graph.
- SQLite schema details.
- Worktree naming and the exclude-file mechanics.
- How `ateam status` renders (plain text is fine).
- Which deterministic checks are discovered automatically from a target repo versus declared in `.ateam/config.yaml`.

## Open questions for the owner

None outstanding. The spike may reopen the rules-under-bare-mode question, which this document already covers both ways.
