# Private Intent Repo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move durable intent (records, contracts, config) out of the target repository into a private git repository per target under ATeam's home, so public targets carry nothing of ATeam's while intent stays versioned, git-native and identical across machines through a private remote.

**Architecture:** `TargetPaths` resolves `.ateam/` content to `~/.ateam/<repo-id>/intent/`, a git repo ATeam creates. The repo id derives from the target's `origin` URL so every clone of the target finds the same intent. Approvals and freezes commit in the intent repo. The worker scratch directory moves out of the worktree to `~/.ateam/<repo-id>/scratch/<contract-id>/`, so the target gets no `.gitignore` edits at all.

**Tech Stack:** unchanged (Python 3.14, click, pydantic, pyyaml, claude-agent-sdk, pytest).

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`, sections "Decisions made by the owner" (2026-10-25 entries), "Intent repo", "Observability and provenance".

## Decisions pending the owner (the plan is written for the recommended option of each)

- **A. Zero footprint in the target.** Recommended: the worker scratch directory lives at `~/.ateam/<repo-id>/scratch/<contract-id>/`, outside the worktree; ATeam never edits the target's `.gitignore`. Alternative: keep `.ateam-scratch/` inside the worktree and require the `.gitignore` line (today's behaviour), which leaves one visible line in the public repo.
- **B. Repo id from the `origin` URL.** Recommended: `sha1(normalised origin URL)[:12]`, falling back to the resolved path when the target has no remote; `ateam doctor` shows the derivation. Alternative: a tracked link file in the target naming the id, which is visible in the public repo.

## Global Constraints

- Everything in the pass-one plan's Global Constraints still holds, except: durable intent lives at `~/.ateam/<repo-id>/intent/` (override root with `ATEAM_HOME`), never under the target; ATeam writes nothing into a target repository.
- The intent repo is a git repository on branch `main`; ATeam commits there with the `ateam:` message prefix and refuses to act when it is dirty.
- Workers may write only inside their worktree (contract paths plus incidental) and inside their own scratch directory; the intent repo and the state store are never writable by a worker.
- Work in a git worktree, test first, no `rm` in worker policy, no clone-local git mechanisms.

## Review Focus

- Two clones of the same target on different paths must resolve to the same intent repo; two different remotes must not collide. (Task 1)
- `ateam intent init` run twice must be idempotent and must refuse to overwrite an existing intent repo with content. (Task 2)
- A worker `Write` to its own scratch directory via an absolute path is allowed; a `Write` to another contract's scratch directory, or to the intent repo, is denied. (Task 5)
- Approval with a dirty intent repo refuses with a message naming the intent repo path, not the target. (Task 3)
- A target whose `origin` is later added or changed: `ateam doctor` warns that the id derivation changed and names both ids; nothing is moved automatically. (Task 6)

---

### Task 1: Repo id from origin; TargetPaths points at the intent repo

**Files:** Modify `src/ateam/paths.py`; Test `tests/test_paths.py`

**Interfaces:**
- `paths.origin_url(root: Path) -> str | None` (`git -C root remote get-url origin`, `None` on failure).
- `paths.normalise_remote(url: str) -> str`: strip scheme, credentials, `git@` user, trailing `/` and `.git`; lowercase host; `git@github.com:a/b.git`, `https://github.com/a/b`, `ssh://git@github.com/a/b.git` all give `github.com/a/b`.
- `paths.repo_id(root) -> str`: `sha1(normalise_remote(origin))[:12]` when origin exists, else `sha1(str(root.resolve()))[:12]`.
- `paths.repo_id_source(root) -> Literal["origin", "path"]`.
- `TargetPaths` gains `intent: Path` (= `home_dir(root) / "intent"`) and `scratch_root: Path` (= `home_dir(root) / "scratch"`); `records`, `contracts`, `plans`, `reports`, `config_file` live under `intent`; `ateam` field removed; `exists()` is true when `intent / ".git"` exists.

- [ ] Write failing tests: `test_normalise_remote_variants`, `test_repo_id_same_for_two_clones_same_origin` (two `git clone`s of a bare repo into different dirs), `test_repo_id_differs_for_different_origins`, `test_repo_id_path_fallback_without_remote`, `test_from_root_points_at_intent`, `test_exists_requires_git_dir`.
- [ ] Run: `.venv/bin/pytest tests/test_paths.py -v` → FAIL (attributes missing).
- [ ] Implement per Interfaces.
- [ ] Run → PASS. Commit: `feat: repo id from origin and intent-repo paths`.

### Task 2: Intent repo lifecycle

**Files:** Create `src/ateam/intent.py`, `tests/test_intent.py`

**Interfaces:**
- `intent.IntentError(Exception)`.
- `intent.init_intent(paths: TargetPaths, remote: str | None = None) -> GitRepo`: creates `paths.intent`, `git init -b main`, `records/`, `contracts/`, `plans/`, `reports/` each with `.gitkeep`, default `config.yaml` via `save_config`, first commit `ateam: initialise intent repo`, sets `origin` when `remote` is given. Idempotent when the repo already exists and is clean; `IntentError` when the directory exists but is not a git repo.
- `intent.intent_git(paths) -> GitRepo` (raises `IntentError` naming `ateam intent init` when missing).
- `intent.set_remote(paths, url)`, `intent.push(paths) -> str`, `intent.pull(paths) -> str` (thin wrappers; `--ff-only` on pull).
- `intent.commit_intent(paths, message, subpaths: list[str]) -> str`: stages only `subpaths` under the intent repo and commits; `IntentError` if nothing staged.

- [ ] Write failing tests: `test_init_creates_repo_dirs_config_and_first_commit`, `test_init_is_idempotent`, `test_init_refuses_non_git_directory`, `test_remote_and_push_pull_against_bare` (bare repo as remote), `test_intent_git_missing_names_init`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: intent repo lifecycle`.

### Task 3: Services commit in the intent repo; fixtures move

**Files:** Modify `src/ateam/records/service.py`, `src/ateam/cli.py` (`_context`, freeze), `tests/conftest.py`, every test referencing `target_repo / ".ateam/..."`

**Interfaces:**
- `RecordService(paths, config)` (no target `GitRepo`): `_guard` requires `intent_git(paths).is_clean()`; commits via `commit_intent(paths, message, ["records"])`. The target-branch guard is removed from approvals.
- `_context` builds `TargetPaths` (which now resolves the intent repo) and keeps `ctx.git` for the target.
- conftest: `target_repo` sets `ATEAM_HOME` (merges the `ateam_home` fixture), calls `init_intent`, and exposes `paths` via a new `target_paths` fixture; `cli_target` writes records and contracts under `paths.records` / `paths.contracts` and commits with `commit_intent`.

- [ ] Write failing tests first for the behaviour changes: `test_approve_commits_in_intent_repo_not_target` (target log unchanged, intent log has `ateam: approve R-0001`), `test_refuses_when_intent_dirty` (message contains the intent path), `test_approve_no_longer_requires_target_branch` (target on another branch still approves).
- [ ] Run → FAIL. Implement; migrate fixtures and existing tests mechanically (`target_repo / ".ateam/records"` → `target_paths.records`, etc.).
- [ ] Run whole suite → PASS. Commit: `feat: approvals and freezes commit in the intent repo`.

### Task 4: Nothing written into the target

**Files:** Modify `src/ateam/runner/run.py` (`prepare_worktree` → `prepare_scratch`), `src/ateam/checkpoint.py`, `src/ateam/roles.py`, `src/ateam/brief.py`; Test `tests/test_run.py`, `tests/test_checkpoint.py`, `tests/test_brief.py`

**Interfaces:**
- `run.prepare_scratch(paths: TargetPaths, contract_id: str) -> Path`: creates `paths.scratch_root / contract_id`, returns it. No `.gitignore` requirement; `WorktreeNotPrepared` removed.
- `checkpoint.load_handoff(scratch_dir: Path)` reads `scratch_dir / "handoff.yaml"`; `collect(...)` takes `scratch_dir`.
- Role prompts and the brief name the absolute handoff path (`Brief.task` gains the line `Handoff file: <path>`); the hard-coded `.ateam-scratch/handoff.yaml` text is gone.
- `treecheck.DEFAULT_IGNORE` drops `.ateam-scratch/**` (keep `.claude/**`).

- [ ] Write failing tests: `test_prepare_scratch_outside_worktree`, `test_handoff_read_from_scratch_dir`, `test_brief_names_absolute_handoff_path`, `test_worktree_has_no_ateam_files_after_run` (fake query; assert no `.ateam-scratch` and no `.ateam` in the worktree).
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: worker scratch lives under ATeam home; target untouched`.

### Task 5: Policy allows the worker's own scratch dir and nothing else outside the worktree

**Files:** Modify `src/ateam/runner/policy.py`, `src/ateam/runner/run.py` (`build_policy`); Test `tests/test_policy.py`

**Interfaces:**
- `Policy` gains `scratch_dir: Path | None`. `relative()` unchanged. New `_decide` rule: a write whose resolved path is inside `scratch_dir` is allowed for BUILD and TEST (INVESTIGATE still never writes); any other path outside the worktree is denied as before. Bash segments mentioning the ATeam home outside `scratch_dir` (the intent repo, `state.db`, `logs/`) are denied.

- [ ] Write failing tests: `test_write_to_own_scratch_allowed_build_and_test`, `test_write_to_other_contract_scratch_denied`, `test_write_to_intent_repo_denied`, `test_bash_touching_intent_repo_denied` (`echo x > ~/.ateam/<id>/intent/records/R-0001.yaml` with the real path).
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: policy permits only the worker's own scratch outside the worktree`.

### Task 6: CLI `intent` group, doctor, status, messages

**Files:** Modify `src/ateam/cli.py`; Test `tests/test_cli.py`

**Interfaces:**
- `ateam intent init [--remote URL]`, `ateam intent path`, `ateam intent remote URL`, `ateam intent push`, `ateam intent pull`.
- `_context` bootstrap message: `No intent repo for <target>. Run: ateam intent init` (exit 2).
- `ateam doctor` prints `repo id <id> (from origin <normalised-url> | from path)`, the intent path, its remote if any, and `WARN` when the state store's recorded id source differs from the current one (store the derivation in `state.db` table `meta(key, value)` on first use).
- `worker run`: the worktree ownership check is unchanged; it no longer calls `prepare_worktree`.

- [ ] Write failing tests: `test_intent_init_then_status_ok`, `test_status_without_intent_names_init`, `test_doctor_shows_id_source_and_intent_path`, `test_doctor_warns_when_id_source_changed`, `test_worker_run_leaves_target_and_worktree_free_of_ateam_files` (refusal path is enough; assert no files created).
- [ ] Run → FAIL. Implement. Run → PASS. Commit: `feat: ateam intent commands, doctor derivation, bootstrap message`.

### Task 7: Docs, examples, live test

**Files:** Modify `README.md`, `docs/first-run-walkthrough.md`, `examples/CLAUDE.target.md`, `examples/config.example.yaml`, `tests/live/test_worker_live.py`, spec "Target repo layout" section (replace with a pointer to "Intent repo").

- [ ] README: setup steps become `ateam intent init [--remote]` then write records under `$(ateam intent path)/records/`; remove every `.gitignore` instruction; explain the private remote and `ateam intent push/pull` for a second machine. Walkthrough: same, with expected output; the "No rm" and binding probes unchanged. `examples/CLAUDE.target.md`: `.ateam/` no longer exists in the target; point at `ateam intent path`; keep the rules. Live fixture: use `init_intent` and `target_paths`.
- [ ] `ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live -s` → PASS, cost under 0.05 dollars.
- [ ] `.venv/bin/pytest -q` → PASS. Commit: `docs: intent repo setup, walkthrough and target snippet`.
