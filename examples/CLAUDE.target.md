# This repository is managed with ATeam

ATeam (local copy at `~/Documents/GitHub/ateam`) keeps this repo's engineering intent in a private intent repo outside this checkout, at the path printed by `ateam intent path` (under `~/.ateam/<repo-id>/intent/`), and runs role-separated workers against this repo. Nothing of ATeam's is tracked here. If you are a Claude session opened by a person in this checkout, these rules apply to you. ATeam's own workers run in a clean room and never read this file.

## What lives in the intent repo

- `records/`: decisions, invariants and conventions, one YAML file each. Only records with `status: approved` are binding. `proposed` ones are suggestions awaiting the owner.
- `contracts/`: task contracts. A contract whose `records:` list is filled and whose status is not `draft` is frozen: its paths, acceptance criteria and bound records are fixed.
- `config.yaml`: target branch, setup commands, deterministic checks, protected paths.

## Rules for a session in this checkout

1. **Never edit the intent repo by hand.** Approve, reject, supersede or retire records with the `ateam records ...` commands; they commit there with an `ateam:` message. A hand edit is detected (`ateam records validate`, `ateam contracts validate`) and stops workers.
2. **Discovery and planning are read-only.** `ateam init` and `ateam plan` run an INVESTIGATE worker in this checkout with no editing tools and a strict shell allow-list; a snapshot before and after guarantees nothing changed. Do not run them with uncommitted work you cannot afford to see in a report.
3. **Read the binding records before changing bound files.** `ateam records list --status approved` shows each record and the paths it binds. If a change you are asked to make conflicts with an approved record, say so and propose a new record instead of working around it. Nothing inferred from code becomes binding without the owner's approval.
4. **A frozen contract is the definition of done for its task.** Do not change its acceptance criteria or paths to make work fit. If the contract is wrong, the owner re-freezes it deliberately (`ateam contracts freeze C-XXXX --force`).
5. **Worker branches are `ateam/<contract-id>` in worktrees outside this checkout.** Do not commit to them from here, and do not run `ateam worker run` with `--cwd` pointing at this checkout; it refuses.
6. **Deletion is an integration-time action.** Do not `rm` tracked files during task work; use `git rm` so the removal is in the reviewed diff.
7. **Workers' scratch and handoff files live under `~/.ateam/<repo-id>/scratch/`**, never in this repo. If you see ATeam files here, something is wrong; say so.
8. **Approvals commit in the intent repo, never here.** Nothing you run through `ateam` changes this checkout's history except your own merges of worker branches.

## Useful commands

```bash
ateam intent path                 # where the records and contracts live
ateam status                      # plans, contract states and latest attempts
ateam plans list                  # feature requests and their state
ateam records review              # the owner's walk-through of proposed records
ateam records validate            # 0 error(s) expected
ateam records list [--status approved]
ateam contracts show C-0001
ateam brief C-0001 --role build   # exactly what a worker is told
ateam schema records|contracts|config
```

Keep this file short. Add a line here when a session in this repo gets something wrong that a rule would have prevented.
