# Working on ATeam

ATeam is an engineering harness around headless Claude Code: durable intent in `.ateam/` records and contracts, role-separated workers in git worktrees, a policy gate, and two human approval gates. Design: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md` (binding). Current plan and status: `README.md`, `docs/superpowers/plans/`. Hands-on guide: `docs/first-run-walkthrough.md`.

## Decisions that are the owner's, not yours

These came from corrections the owner made after work was built. Do not re-decide them, and do not make decisions of the same kind on the owner's behalf.

1. **Work in git worktrees.** Every change to this repo, however small, happens in a worktree under `../ateam-worktrees/<branch>/`, never on a branch in the main checkout. Merge back, remove the worktree, delete the branch.
2. **Git-native mechanisms only.** Nothing may depend on clone-local state (`.git/info/exclude`, local git config, per-machine files). A fresh clone on another machine must behave identically. Ignore rules go in a tracked `.gitignore`. Keep git's own semantics (for example, `git diff <base>` includes uncommitted edits); do not invent a parallel convention.
3. **Workers never run `rm`.** Deletion is an integration-time action. Tracked files are removed with `git rm` so the change is in the reviewed diff. The policy denies `rm` in every form; do not loosen it.
4. **A contract is binding or work does not start.** No worker runs unless the contract is frozen, unchanged since the freeze, runnable, and free of record drift. Do not add bypasses.
5. **Nothing inferred becomes binding without the owner.** Records proposed from code are `proposed` until the owner approves them.

## Ask before deciding when a choice is one of these

Decide and record only mechanics: data structures, test fixtures, message wording, file layout within a module. Stop and ask, with a recommendation, before building anything that:

- sets how work is organised (branches, worktrees, merge flow);
- leaves a side effect in a target repository or varies between clones or machines;
- defines what a worker is permitted or forbidden to do;
- changes an approval gate, a state machine transition, or what counts as "done";
- narrows or widens a policy because a reviewer called it a false positive.

If a delegated mechanism fails, the fallback is a question, not a different mechanism with wider consequences. Say each ruling in chat at the moment you make it, not only in a list at the end.

## Conventions

- Python 3.14, `.venv/` in the repo. Run everything with `.venv/bin/...`. Tests: `.venv/bin/pytest` (unit, no API calls). Live: `ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live -s` (one real haiku session, opt in only).
- Test first. Every behaviour change starts with a failing test you watched fail. Table-driven tests for the policy (`tests/test_policy.py`) are the place for new deny and allow strings.
- Worker sessions use the clean room (`setting_sources=[]`, strict MCP config, slash commands off, auto-memory off, permission mode `default`). Never `--bare`: it does not authenticate with a claude.ai login.
- Never `str.strip()` the output of `git status --porcelain`.
- Approval commits in a target stage only `.ateam/` paths and use the `ateam:` message prefix.
- This repo is nested inside an unrelated parent git repo at `~/Documents/GitHub`. Confirm `git rev-parse --show-toplevel` ends in `/ateam` before any git command; never stage the parent.
- `docs/2026-10-25-product-*.md` were written by another session; leave them unless asked.
- End commit messages with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
