# ATeam

ATeam is an engineering harness around headless Claude Code. It keeps engineering intent in version-controlled records and task contracts in a private intent repo per target, runs role-separated workers (BUILD, INVESTIGATE, TEST) in isolated git worktrees through the Claude Agent SDK, and verifies the worktree itself rather than trusting a worker's report. The owner approves what gets built and what gets merged; ATeam decides how to build and verify in between.

Design: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`. Plans: `docs/superpowers/plans/`.

Durable intent never lives in the target repository. Each target gets a private git repository under `~/.ateam/<repo-id>/intent/` (records, contracts, config), created by `ateam intent init` and optionally pushed to a private remote so another machine pulls the same intent. The repo id derives from the target's `origin` URL, so every clone finds the same intent. The target carries nothing of ATeam's: no `.ateam/`, no ignore lines, no scratch files.

## Setup

Requires Python 3.14 and a Claude Code login (`claude auth status` should say logged in). A claude.ai subscription login works; an API key is not needed.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ateam --version
```

Workers run in a clean room built from SDK options (no setting sources, strict MCP config, slash commands disabled, auto-memory off). Claude Code's `--bare` flag is deliberately not used: it authenticates only with an API key.

## Setting up a fresh target repo

Install once per machine (above). Then, per target repository; everything ATeam writes ends up committed in the target's private intent repo, never in the target:

1. **Bootstrap.** `ateam intent init [--remote git@github.com:you/private-intent.git]` creates the intent repo. Optionally copy `examples/CLAUDE.target.md` to the target's `CLAUDE.md` so a Claude session a person opens there knows the rules. Edit `$(ateam intent path)/config.yaml` to name the target branch and the deterministic checks the planner may use.
2. **Discover.** `ateam init` runs a read-only INVESTIGATE worker in your checkout and proposes a small baseline of records, each marked documented or inferred, requirement or observation, with evidence and the paths it binds. `ateam records review` walks you through them: approve as requirement or observation, reject with a reason, or skip. Rejected proposals never come back. The brief the worker starts with carries the target's CLAUDE.md and AGENTS.md, the proposed records and the plan index, and `brief_docs` in the config adds more documents.
3. **Plan.** `ateam plan "<request>"` runs at `--tier careful` unless you pass another. The planner's brief carries the target's rules with a note on their scope, the records, and an index of existing plans with their acceptance criteria and interfaces; `brief_docs` in the config adds documents. It drafts contracts with dependencies, acceptance criteria, the interfaces each contract produces and consumes (exact names and signatures, frozen with the contract and shown in every brief), a complexity, risk and uncertainty allocation with its reasoning, and any records the plan introduces. A consumed name that no earlier contract produces and the code does not define is rejected or flagged at planning. A second session, a staff-engineer reviewer, then judges the draft. Blocking or important findings earn one revision round (no second review); in-plan references the planner writes as `contract N` are stored as contract ids. The review (`verdict`, findings, whether the plan was revised) is stored with the plan, printed by `ateam plan` and shown again by `ateam plans show`. A plan costs up to three sessions: plan, review, revision. `--no-review` skips the review; `--tier standard` or `light` makes every session cheaper. If the review session fails, the plan is saved unreviewed with verdict `unavailable`; an interrupt (Ctrl-C) during the review also saves the plan unreviewed, while one during the planner session records the plan `plan_rejected` (`interrupted`); the next `ateam plan` also rejects a plan a killed process left at `investigating`. Run one `ateam plan` per target at a time, and don't create branches or worktrees while a plan or review session runs: the before-and-after snapshot covers the intent repo and refs, so another command's commit or a new branch discards the session's report as tamper. If a live plan already covers the request, the outcome is `plan_covered`: no contracts are written and the covering plans are named. `--replaces P-0001` replaces a `plan_proposed` plan, or an approved one none of whose contracts has started (no attempt, transition, checkpoint or verify marker): the old one is hidden from the new planner's index, and approving the new plan marks the old one `superseded`, with its contracts when it was approved. A superseded contract never runs, verifies or freezes again, and `contracts freeze` refuses any contract of a rejected, covered or superseded plan. A planning session that changes the intent repo is discarded as tamper, and only the exact files ATeam wrote are committed. `ateam approve P-0001 [--tier C-0002=careful]` walks the proposed records, applies your tier overrides, freezes the contracts in dependency order and marks the plan approved. `ateam reject P-0001 "reason"` records why.
4. **Run a worker in a worktree.** `git worktree add ../repo-C-0001 -b ateam/C-0001 main`, then `ateam worker run C-0001 --role build --cwd ../repo-C-0001`. The commands in the config's `setup:` list run once per worktree, before the first attempt there; a failing setup command stops the run, and the next run tries setup again. Read the report, the diff and `ateam status`. A TEST report (`--role test`) carries `requirements_unclear`. When a verdict is `not_met`, the run prints `classification: failed/requirements` if the report says the requirements were unclear and `failed/implementation` otherwise. A `not_met` verdict exits 5; otherwise the exit code reflects whether the run was valid (0, 3 for a scope breach, 1 for no report, 4 when the contract does not bind).
5. **Verify, refresh, integrate, merge.** Commit the work in the worktree, then `ateam verify C-0001 --cwd ../repo-C-0001`. This is the minimal verifier: it runs the contract's frozen checks in the worktree with a scrubbed environment (no credentials, no inherited virtualenv; the worktree needs its own `.venv` if the checks use one), checks the change stays inside the contract's paths, checks that every `test` acceptance criterion names a test that exists, and fails as `tamper` if the checks move the worktree's HEAD or change its tracked files, touch the intent repo or the state store, or move your checkout's HEAD (new untracked files such as build output, and your own edits in your checkout, are reported as warnings). It records `verified` or `failed` (class `environment` or `implementation`) in the intent repo and then the state store, marked `verifier: minimal`. It does not judge `judgment` criteria; run a TEST worker (`--role test`) for those. A verify that is interrupted after judging is finished later only when its verdict was `failed`; a `verified` verdict is always earned again by a run that completes its own checks, and a verify stopped while its checks ran is recorded as `failed (tamper)`. Known residual, accepted by the owner for the bootstrap: the checks run as you, so a check that rewrites the intent repo and the state store and then kills `ateam verify` before it compares its snapshots can forge state; review the diff before you merge. The full verifier (plan P-0002) is where that is closed. When a contract is verified, `ateam contracts refresh` moves every frozen draft whose dependencies are all verified to `ready` (status only; a draft whose bound records drifted stays draft and is reported, exit 1). When every contract of the plan is verified, `ateam integrate P-0001` creates `ateam/P-0001` at the target tip (recorded before the branch is made), checks it out in a worktree under ATeam's home (`<home>/worktrees/P-0001`, setup once), merges each contract's verified head with a merge commit in dependency order, merges the target in if it moved (never rebases), runs `config.checks` plus every contract's checks there, and writes the merge proposal `reports/P-0001-merge.md` to the intent repo (diff summary, evidence per contract, records, unresolved risks including unchecked judgment criteria, telemetry). A conflict or a setup failure pauses the plan with `environment_failure` (exit 3 or 1), failed checks with `verification_unresolvable` (exit 1); a refusal changes nothing (exit 4). A re-run replays any store step an interruption lost, and on a proposed plan whose target moved it merges the new tip and proposes again. `ateam merge P-0001`, in your checkout on the target branch with nothing uncommitted (untracked files count), reads the committed proposal and fast-forwards the target to its tip; if the target moved it refuses (exit 1) and says to re-run integrate. Nothing is pushed or fetched; the integration branch and worktree stay for a later cleanup. `ateam reject P-0001 "reason"` also rejects a merge proposal. Scheduling (`ateam run`), pause and resume, and the full verifier with the TEST role are the next stages.

`docs/first-run-walkthrough.md` is the same sequence on a scratch repo with the expected output at every step. Records and contracts can also be written by hand from `examples/` when you want to seed intent directly.

## Using it for development today

Discovery and planning are automated and gated: ATeam proposes records and contracts, you approve them. You are still the scheduler and the integrator. ATeam guarantees that a worker starts only under a frozen, unchanged contract, sees the binding records up front and again when it touches a bound file, cannot edit protected files, push, `rm` or leave its declared paths, and reports in a typed schema that the tree and scope check cross-examine. Telemetry per attempt (tier, model, tokens, cost, turns, wall time, outcome) accumulates in the state store for calibrating the tiers.

Not yet automated: scheduling several workers, running the deterministic checks plus the TEST role as a gate, integrating onto a branch with a merge proposal for approval, event logs and the tmux viewer, and the doctor's repair modes. Every interface they need exists.

A good first real use: a small, well-understood feature in a throwaway clone of one of your repos. Three records, two contracts, one BUILD run each, then a TEST run on the same worktree with `--role test --tier light` to see the verdict format.

ATeam is not the only path. Where it errors, or where a capability is not built yet, fall back to the other parts of Claude Code (a plain session in the checkout, the superpowers workflow) and come back when it is useful again. Records and contracts in the intent repo stay valid either way.

Everything here is readable by an agent as well as a person: markdown, YAML and the JSON Schema from `ateam schema`. Workers themselves run in a clean room and read none of it; they receive only the brief ATeam assembles.

## Commands

Every command except `ateam schema` takes `--target PATH` (default: the current directory). All but `intent init`, `intent path` and `doctor` require the target's intent repo to exist.

```bash
ateam doctor --target ~/code/myrepo            # versions, repo id derivation, intent repo, target branch
ateam intent init [--remote URL]               # create the private intent repo for this target
ateam intent path | remote URL | push | pull   # where it is; private remote; sync with another machine
ateam init [--max-candidates 12] [--tier careful]   # discover and propose records (read-only worker in your checkout)
ateam records review [--all]                   # approve as requirement/observation, reject with reason, skip
ateam plan "<request>" [--tier careful] [--replaces P-0001] [--no-review]   # propose contracts and a plan, review it with a second session; may end as plan_covered
ateam approve P-0001 [--tier C-0002=careful] [--tier C-0002:investigate=light] [--skip-records]   # marks a replaced plan (and an approved one's contracts) superseded
ateam reject P-0001 "reason"                   # a proposed plan, a merge proposal, or an approved plan that has not started
ateam contracts refresh [P-0001]               # draft -> ready once every dependency is verified; stops on drift
ateam integrate P-0001                         # integration branch, merges, full checks, merge proposal (0 / 1 / 3 / 4)
ateam merge P-0001                             # fast-forward the target in your checkout; never pushes (0 / 1 / 4)
ateam plans list | show P-0001               # show prints the review too
ateam records validate --target ~/code/myrepo  # schema, globs, supersession, dead globs, hand edits
ateam records list [--status approved]
ateam records show R-0001
ateam records approve R-0001 [--basis requirement|observation]   # shows overlapping approved records first
ateam records reject R-0001 "reason"
ateam records supersede R-0001 --with R-0002   # one transaction, one commit
ateam records retire R-0001 "reason"
ateam contracts show C-0001
ateam contracts freeze C-0001 [--force]        # selects bound records, snapshots their hashes; binding from here on
ateam contracts validate                       # hand edits and record drift against the freeze
ateam schema records|contracts|config          # JSON Schema for editors
ateam brief C-0001 --role build                # print the brief a worker would receive
ateam worker run C-0001 --role build --cwd /path/to/worktree [--base REF] [--tier light] [--resume ID]   # runs config.setup once per worktree; base defaults to the merge-base with the target branch
ateam status
```

Approval and freeze commands commit in the intent repo with an `ateam:` message and refuse to act when it has uncommitted changes. They never touch the target.

## Your first record and contract

`ateam init` and `ateam plan` write records and contracts for you (step 2 and 3 above). To seed the intent repo by hand instead, copy the annotated templates. Annotated templates live in [`examples/`](examples/): [`record.example.yaml`](examples/record.example.yaml), [`contract.example.yaml`](examples/contract.example.yaml), [`plan.example.yaml`](examples/plan.example.yaml), [`config.example.yaml`](examples/config.example.yaml), and [`CLAUDE.target.md`](examples/CLAUDE.target.md). A test loads them through the real models, so they cannot drift from the schema. `ateam schema records|contracts|config` prints the JSON Schema for editor validation.

```bash
cd ~/code/myrepo                                   # a throwaway clone is the safest first target
ateam intent init                                  # private intent repo under ~/.ateam/<repo-id>/intent/
I=$(ateam intent path)
cp ~/code/ateam/examples/record.example.yaml   "$I/records/R-0001-reading-state.yaml"
cp ~/code/ateam/examples/contract.example.yaml "$I/contracts/C-0001-reading-state-api.yaml"
# edit both: statement, evidence, binds.paths, contract paths and acceptance criteria for your repo
git -C "$I" add -A && git -C "$I" commit -m "ateam: first record and contract"

ateam records validate                              # must print 0 error(s)
ateam records approve R-0001                        # commits in the intent repo
ateam contracts freeze C-0001                       # selects bound records, snapshots hashes, commits; binding from here on
ateam brief C-0001 --role build                     # exactly what the worker will be told

git worktree add ../myrepo-C-0001 -b ateam/C-0001 main
ateam worker run C-0001 --role build --cwd ../myrepo-C-0001 --tier light
ateam status
```

A worker refuses to start (exit code 4) unless the contract is frozen, unchanged since the freeze, in a runnable state, and none of its frozen records have changed. `ateam contracts validate` reports hand edits and record drift; `ateam contracts freeze --force` re-freezes deliberately. Workers refuse to run in the target checkout itself: `--cwd` must be a worktree of the target, outside it.

The worker's handoff notes live in its scratch directory under `~/.ateam/<repo-id>/scratch/<contract-id>/`, never in the worktree, so nothing of ATeam's ever appears in `git status` of the target. Workers may not run `rm` at all; a tracked file is removed with `git rm`, which shows in the diff, and cleanup belongs to integration. Every role may `cd` only to an absolute path inside its worktree or its scratch directory; a command the analyser cannot parse is denied. Workers run under `/bin/bash` with `CDPATH`, `BASH_ENV` and `ENV` set to empty (the SDK passes the parent environment through, so unsetting is not enough); commands are read under both the bash 3.2 and 5.3 grammars. Functions and aliases that Claude Code carries over from your shell profile are not controlled by the policy.

## Layout

```
~/.ateam/<repo-id>/
  intent/            private git repo: config.yaml, records/, contracts/, plans/ (pass two), reports/ (pass two)
  state.db           operational state (SQLite): state machines, attempts, telemetry, checkpoints, run lock; never authoritative
  scratch/<C-id>/    the worker's handoff notes
~/.ateam/targets.json   which target path maps to which id and how the id was derived (doctor warns on change)
<target>/            nothing of ATeam's
```

Override the home with `$ATEAM_HOME`.

## Tests

```bash
.venv/bin/pytest                      # unit suite, no API calls
ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live -s   # one real haiku session, a few cents
```

## Status

Done: records, contracts, config, the private intent repo, state store, git operations, brief assembly, the worker policy gate, the clean-room runner, a single verified worker run, discovery (`ateam init`), the planner (`ateam plan`) and plan approval. Next: the scheduler and worktree pool, the verifier (deterministic checks plus the TEST role as a gate), the integrator with the merge gate, event logs with the tmux viewer, the doctor's repair modes, calibration.

The spike that validated the SDK integration is in `spikes/2026-10-24-sdk-bare-hooks/`.
