# ATeam

ATeam is an engineering harness around headless Claude Code. It keeps engineering intent in version-controlled records and task contracts inside a target repository, runs role-separated workers (BUILD, INVESTIGATE, TEST) in isolated git worktrees through the Claude Agent SDK, and verifies the worktree itself rather than trusting a worker's report. The owner approves what gets built and what gets merged; ATeam decides how to build and verify in between.

Design: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`. Pass-one plan: `docs/superpowers/plans/2026-10-24-ateam-foundations.md`.

## Setup

Requires Python 3.14 and a Claude Code login (`claude auth status` should say logged in). A claude.ai subscription login works; an API key is not needed.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ateam --version
```

Workers run in a clean room built from SDK options (no setting sources, strict MCP config, slash commands disabled, auto-memory off). Claude Code's `--bare` flag is deliberately not used: it authenticates only with an API key.

## Setting up a fresh target repo

Install once per machine (above). Then, per target repository, five steps, all ending up committed in that repo:

1. **Bootstrap.** `mkdir -p .ateam/records .ateam/contracts`; add `.ateam-scratch/` to the tracked `.gitignore`; copy `examples/CLAUDE.target.md` to `CLAUDE.md` so a Claude session a person opens there knows `.ateam/` is managed through the CLI; optionally copy `examples/config.example.yaml` to `.ateam/config.yaml` to name the target branch and the deterministic checks.
2. **First records.** Copy `examples/record.example.yaml` once per decision or invariant you already hold, bound to the paths it governs. A handful, not fifty. `ateam records validate`, then `ateam records approve R-0001` for each; approval commits only `.ateam/`.
3. **One contract per task.** Copy `examples/contract.example.yaml`: the paths a worker may touch, the acceptance criteria and their checks, the allocation tier. `ateam contracts freeze C-0001` selects the binding records and makes the contract binding. Commit.
4. **Run a worker in a worktree.** `git worktree add ../repo-C-0001 -b ateam/C-0001 main`, then `ateam worker run C-0001 --role build --cwd ../repo-C-0001`. Read the report, the diff and `ateam status`.
5. **Verify and merge yourself.** Run the contract's checks in the worktree, optionally a TEST worker (`--role test`), then merge the branch. This loop is manual until pass two.

`docs/first-run-walkthrough.md` is the same sequence on a scratch repo with the expected output at every step.

## Using it for development today

In pass one you are the planner and the integrator; ATeam is the enforcer. You decide the records and write the contracts. ATeam guarantees that a worker starts only under a frozen, unchanged contract, sees the binding records up front and again when it touches a bound file, cannot edit protected files, push, `rm` or leave its declared paths, and reports in a typed schema that the tree and scope check cross-examine. Telemetry per attempt (tier, model, tokens, cost, turns, wall time, outcome) accumulates in the state store for calibrating the tiers.

Not yet automated: discovering records from an existing codebase (`ateam init`), breaking a feature into contracts (`ateam plan`), scheduling several workers, running the deterministic checks plus the TEST role as a gate, and integrating onto a branch with a merge proposal for approval. Those are pass two; every interface they need exists.

A good first real use: a small, well-understood feature in a throwaway clone of one of your repos. Three records, two contracts, one BUILD run each, then a TEST run on the same worktree with `--role test --tier light` to see the verdict format.

Everything here is readable by an agent as well as a person: markdown, YAML and the JSON Schema from `ateam schema`. Workers themselves run in a clean room and read none of it; they receive only the brief ATeam assembles.

## Commands

Every command except `ateam schema` takes `--target PATH` (default: the current directory), which must contain an `.ateam/` directory.

```bash
ateam doctor --target ~/code/myrepo            # versions, target branch, workspace health
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
ateam worker run C-0001 --role build --cwd /path/to/worktree [--base REF] [--tier light] [--resume ID]   # base defaults to the merge-base with the target branch
ateam status
```

Approval commands commit only `.ateam/` paths on the configured target branch in your checkout, and refuse to act when the checkout is on another branch or `.ateam/` already has uncommitted changes.

## Your first record and contract

There is no `ateam init` or `ateam plan` yet (pass two), so the first record and contract are written by hand. Annotated templates live in [`examples/`](examples/): [`record.example.yaml`](examples/record.example.yaml), [`contract.example.yaml`](examples/contract.example.yaml), [`config.example.yaml`](examples/config.example.yaml), and [`CLAUDE.target.md`](examples/CLAUDE.target.md), a `CLAUDE.md` for the target repo so a Claude session a person opens there knows `.ateam/` is managed through the CLI. A test loads them through the real models, so they cannot drift from the schema. `ateam schema records|contracts|config` prints the JSON Schema for editor validation.

```bash
cd ~/code/myrepo                                   # a throwaway clone is the safest first target
mkdir -p .ateam/records .ateam/contracts
echo '.ateam-scratch/' >> .gitignore                # the worker's handoff dir; tracked, so every clone agrees
cp ~/code/ateam/examples/CLAUDE.target.md CLAUDE.md # rules for human-driven Claude sessions in this repo (workers never read it)
cp ~/code/ateam/examples/record.example.yaml   .ateam/records/R-0001-reading-state.yaml
cp ~/code/ateam/examples/contract.example.yaml .ateam/contracts/C-0001-reading-state-api.yaml
# edit both: statement, evidence, binds.paths, contract paths and acceptance criteria for your repo
git add .ateam && git commit -m "ateam: first record and contract"

ateam records validate                              # must print 0 error(s)
ateam records approve R-0001                        # commits only .ateam/ on the target branch
ateam contracts freeze C-0001                       # selects bound records, snapshots hashes; the contract is now binding
ateam brief C-0001 --role build                     # exactly what the worker will be told

git worktree add ../myrepo-C-0001 -b ateam/C-0001 main
ateam worker run C-0001 --role build --cwd ../myrepo-C-0001 --tier light
ateam status
```

A worker refuses to start (exit code 4) unless the contract is frozen, unchanged since the freeze, in a runnable state, and none of its frozen records have changed. `ateam contracts validate` reports hand edits and record drift; `ateam contracts freeze --force` re-freezes deliberately. Workers also refuse to run in the target checkout itself: `--cwd` must be a linked worktree.

`ateam worker run` creates `.ateam-scratch/` in the worktree for the worker's handoff notes and refuses to start (exit code 4) unless the target's tracked `.gitignore` already ignores it. ATeam never uses clone-local files such as `.git/info/exclude`, so a fresh clone on another machine behaves identically. Workers may not run `rm` at all; a tracked file is removed with `git rm`, which shows in the diff, and cleanup belongs to integration.

## Target repo layout

```
<target>/.ateam/
  config.yaml      target_branch, setup, env, checks, serialize_checks, protected_paths, max_workers, tiers
  records/         R-0001-<slug>.yaml  (decision | invariant | convention)
  contracts/       C-0001-<slug>.yaml
  plans/           P-0001-<slug>.yaml  (pass two)
  reports/         merge proposals      (pass two)
```

Operational state (SQLite: state machines, attempts, telemetry, checkpoints, run lock) lives at `~/.ateam/<repo-id>/state.db`, or under `$ATEAM_HOME`. It is never authoritative.

## Tests

```bash
.venv/bin/pytest                      # unit suite, no API calls
ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live -s   # one real haiku session, a few cents
```

## Status

Pass one is complete: records, contracts, config, state store, git operations, brief assembly, the worker policy gate, the clean-room runner and a single verified worker run. Discovery (`ateam init`), the planner (`ateam plan`), the scheduler and worktree pool, the verifier (deterministic checks plus the TEST role) and the integrator with the merge gate are pass two.

The spike that validated the SDK integration is in `spikes/2026-10-24-sdk-bare-hooks/`.
