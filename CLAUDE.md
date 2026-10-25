# Working on ATeam

ATeam is an engineering harness around headless Claude Code: durable intent in `.ateam/` records and contracts, role-separated workers in git worktrees, a policy gate, and two human approval gates. Design: `docs/superpowers/specs/2026-10-24-ateam-harness-design.md` (binding). Current plan and status: `README.md`, `docs/superpowers/plans/`. Hands-on guide: `docs/first-run-walkthrough.md`.

## Decisions that are the owner's, not yours

These came from corrections the owner made after work was built. Do not re-decide them, and do not make decisions of the same kind on the owner's behalf.

1. **Work in git worktrees.** Every change to this repo, however small, happens in a worktree under `../ateam-worktrees/<branch>/`, never on a branch in the main checkout. Merge back, remove the worktree, delete the branch.
2. **Git-native mechanisms only.** Nothing may depend on clone-local state (`.git/info/exclude`, local git config, per-machine files). A fresh clone on another machine must behave identically. Ignore rules go in a tracked `.gitignore`. Keep git's own semantics (for example, `git diff <base>` includes uncommitted edits); do not invent a parallel convention.
3. **Workers never run `rm`.** Deletion is an integration-time action. Tracked files are removed with `git rm` so the change is in the reviewed diff. The policy denies `rm` in every form; do not loosen it.
4. **A contract is binding or work does not start.** No worker runs unless the contract is frozen, unchanged since the freeze, runnable, and free of record drift. Do not add bypasses.
5. **Nothing inferred becomes binding without the owner.** Records proposed from code are `proposed` until the owner approves them; plan approval freezes contracts but never approves a record by itself.
6. **Read-only investigations run in the owner's checkout; enforcement is pre-guard first.** `ateam init` and `ateam plan` use INVESTIGATE sessions with no editing tools, a shell allow-list in which no interpreter, stream editor or shell is listed and substitutions, `-exec` forms, pagers and output flags are denied, and a before-and-after snapshot (HEAD, refs, stash list, dirty paths and their content) that marks any change `tamper`. The system prompt explains the role and is never the control.

## Ask before deciding when a choice is one of these

Decide and record only mechanics: data structures, test fixtures, message wording, file layout within a module. Stop and ask, with a recommendation, before building anything that:

- sets how work is organised (branches, worktrees, merge flow);
- leaves a side effect in a target repository or varies between clones or machines;
- defines what a worker is permitted or forbidden to do;
- changes an approval gate, a state machine transition, or what counts as "done";
- narrows or widens a policy because a reviewer called it a false positive.

If a delegated mechanism fails, the fallback is a question, not a different mechanism with wider consequences. Say each ruling in chat at the moment you make it, not only in a list at the end.

## Working style

Adapted from the Claude Code team's own published practices (Boris Cherny's "how I use Claude Code" threads, January and February 2026). Kept to what fits this repo.

- **Plan first, then execute.** Any task bigger than a one-file change starts in plan mode. Iterate on the plan until it is right, have a second fresh Claude review it as a staff engineer, then implement. If a task goes sideways, stop pushing and re-plan. Plan the verification steps too, not only the build.
- **Give Claude a way to verify its own work.** Every change has a check it can run: the unit suite, the table-driven policy tests, the opt-in live run, or `ateam worker run` against a scratch repo. "Done" means the check ran and you read its output, never "should pass".
- **This file is the memory of mistakes.** Whenever the owner corrects something, or a reviewer finds a class of bug, add a line here in the same change, under the heading it belongs to. Keep it short enough to read in a minute; cut a line when it stops earning its place.
- **Subagents for repeated and context-heavy work.** Explore with subagents to keep the main context clean; use a fresh reviewer for whole-branch review; simplify after a feature is done. Anything done more than once a day becomes a skill or a slash command under `.claude/`, committed to git.
- **Pre-allow permissions, never skip them.** Known-safe commands go in `.claude/settings.json` so they are shared; no `--dangerously-skip-permissions`.
- **Make the model justify changes.** Ask why a fix is right, and ask for a rewrite when a fix is mediocre rather than patching it further. Detailed specs beat clever prompts.
- **Parallel work is one worktree per session.** Never two sessions in one checkout.
- **Model choice is the owner's.** Default to the strongest model with thinking for design, review and policy work; a smaller model is fine for mechanical, well-specified tasks and for live tests.

## Conventions

- Python 3.14, `.venv/` in the repo. Run everything with `.venv/bin/...`. Tests: `.venv/bin/pytest` (unit, no API calls). Live: `ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live -s` (one real haiku session, opt in only).
- Test first. Every behaviour change starts with a failing test you watched fail. Table-driven tests for the policy (`tests/test_policy.py`) are the place for new deny and allow strings.
- Worker sessions use the clean room (`setting_sources=[]`, strict MCP config, slash commands off, auto-memory off, permission mode `default`). Never `--bare`: it does not authenticate with a claude.ai login.
- Never `str.strip()` the output of `git status --porcelain`.
- Approval commits in a target stage only `.ateam/` paths and use the `ateam:` message prefix.
- This repo is nested inside an unrelated parent git repo at `~/Documents/GitHub`. Confirm `git rev-parse --show-toplevel` ends in `/ateam` before any git command; never stage the parent.
- `docs/2026-10-25-product-*.md` were written by another session; leave them unless asked.
- End commit messages with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
