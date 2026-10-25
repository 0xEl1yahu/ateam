# This repository is managed with ATeam

ATeam (https://github.com/0xEl1yahu/ateam, local copy at `~/Documents/GitHub/ateam`) keeps this repo's engineering intent under `.ateam/` and runs role-separated workers against it. If you are a Claude session opened by a person in this checkout, these rules apply to you. ATeam's own workers run in a clean room and never read this file.

## What lives under `.ateam/`

- `records/`: decisions, invariants and conventions, one YAML file each. Only records with `status: approved` are binding. `proposed` ones are suggestions awaiting the owner.
- `contracts/`: task contracts. A contract whose `records:` list is filled and whose status is not `draft` is frozen: its paths, acceptance criteria and bound records are fixed.
- `config.yaml`: target branch, setup commands, deterministic checks, protected paths.

## Rules for a session in this checkout

1. **Never edit `.ateam/` by hand.** Approve, reject, supersede or retire records with the `ateam records ...` commands; they commit only `.ateam/` paths with an `ateam:` message. A hand edit is detected (`ateam records validate`, `ateam contracts validate`) and stops workers.
2. **Read the binding records before changing bound files.** `ateam records list --status approved` shows each record and the paths it binds. If a change you are asked to make conflicts with an approved record, say so and propose a new record instead of working around it. Nothing inferred from code becomes binding without the owner's approval.
3. **A frozen contract is the definition of done for its task.** Do not change its acceptance criteria or paths to make work fit. If the contract is wrong, the owner re-freezes it deliberately (`ateam contracts freeze C-XXXX --force`).
4. **Worker branches are `ateam/<contract-id>` in worktrees outside this checkout.** Do not commit to them from here, and do not run `ateam worker run` with `--cwd` pointing at this checkout; it refuses.
5. **Deletion is an integration-time action.** Do not `rm` tracked files during task work; use `git rm` so the removal is in the reviewed diff.
6. **`.ateam-scratch/` is a worker's handoff directory**, ignored through `.gitignore`. Leave it alone and never commit it.
7. **Approval commits land on the target branch** named in `.ateam/config.yaml` (default `main`), from this checkout. Be on that branch with `.ateam/` clean before running an approval command.

## Useful commands

```bash
ateam status                      # contract states and latest attempts
ateam records validate            # 0 error(s) expected
ateam records list [--status approved]
ateam contracts show C-0001
ateam brief C-0001 --role build   # exactly what a worker is told
ateam schema records|contracts|config
```

Keep this file short. Add a line here when a session in this repo gets something wrong that a rule would have prevented.
