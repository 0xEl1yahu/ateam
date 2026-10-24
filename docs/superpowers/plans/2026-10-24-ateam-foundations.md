# ATeam Foundations Implementation Plan (pass one)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the durable-intent layer (records, contracts, config), the operational state store, the git operations, the brief assembler, the worker policy and runner, and a CLI that can validate and approve records, freeze a contract, and run one role-separated worker in a clean-room Claude session against a worktree.

**Architecture:** A Python package `ateam` with a `click` CLI. Durable intent is YAML under `<target>/.ateam/`, validated with pydantic models. Operational state is SQLite under `~/.ateam/<repo-id>/`. Workers are driven through the Claude Agent SDK with a clean-room option set, a permission callback and PreToolUse hooks as the policy gate, and a JSON schema for the worker's report. Pass two (separate plan) adds discovery, planner, scheduler, verifier and integrator on top of these interfaces.

**Tech Stack:** Python 3.14, `click`, `pydantic>=2`, `pyyaml`, `claude-agent-sdk>=0.2.174,<0.3`, `pytest`, `pytest-asyncio`. SQLite from the standard library. Claude Code CLI 2.1.310 (bundled with the SDK).

**Spec:** `docs/superpowers/specs/2026-10-24-ateam-harness-design.md`

## Global Constraints

- Python 3.14; the repo's `.venv/` is the interpreter for every command below (`.venv/bin/python`, `.venv/bin/pytest`).
- Run every git command from `/Users/elibelilty/Documents/GitHub/ateam`; confirm `git rev-parse --show-toplevel` ends in `/ateam`. Never stage the parent repo at `~/Documents/GitHub`.
- Durable intent lives only under `<target>/.ateam/`. Operational state lives only under `~/.ateam/<repo-id>/` (override with env `ATEAM_HOME`). Nothing operational is written into a target repo.
- Record IDs match `^R-\d{4}$`, contract IDs `^C-\d{4}$`, plan IDs `^P-\d{4}$`. ATeam is the sole allocator; IDs are never reused.
- Record statuses: `proposed | approved | rejected | superseded | retired`. Contract statuses: `draft | ready | building | verifying | verified | failed | paused | integrated`. Feature statuses: `requested | investigating | plan_proposed | plan_approved | plan_rejected | executing | paused | integrating | merge_proposed | merged | abandoned`. Pause reasons: `decision_change_needed | scope_exceeded | budget_exhausted | verification_unresolvable | sensitive_action | environment_failure | records_drifted`.
- Only approved records are ever delivered to a worker.
- Worker sessions always use the clean room: `setting_sources=[]`, `strict_mcp_config=True`, `extra_args={"disable-slash-commands": None}`, `permission_mode="default"`, an environment with every `CLAUDE*` variable removed and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. Never `--bare` (fails with a claude.ai login).
- Never `str.strip()` the output of `git status --porcelain`; the first column is significant.
- Tests never call the Claude API unless env `ATEAM_LIVE_TESTS=1` is set; live tests use model alias `haiku`, `max_turns<=10`, `max_budget_usd<=0.5`.
- Protected paths in every worktree: `.ateam/**` and `.claude/**`. Scope and tree checks ignore `.claude/**` and `.ateam-scratch/**`.
- Commits made by ATeam into a target stage only paths under `.ateam/` and use the message prefix `ateam:`.
- No em-dashes in any user-facing CLI text.

## Review Focus

- A record `binds.paths` entry that is absolute, starts with `./`, ends with `/`, or contains `..` must be rejected by the validator as an error, not silently matched against nothing. (Task 4)
- A record file whose filename ID differs from its `id` field, or two files carrying the same `id`, must make the store raise `RecordStoreError` naming both paths. (Task 3)
- A worker `Edit`/`Write` whose `file_path` is relative, contains `..`, or resolves outside the worktree must be denied by the policy after resolving against the worktree root. (Task 10)
- A Bash command that chains a destructive command after a benign one (`echo ok; git push`, newline-separated, `&&`) must be denied; the destructive pattern scan covers the whole command text. (Task 10)
- A worker-written `handoff.yaml` that is empty, not valid YAML, or not a mapping must load as "no notes" with a warning, never raise. (Task 9)

---

## File structure

```
pyproject.toml
src/ateam/__init__.py            __version__, SPIKED_CLI_VERSION = "2.1.310"
src/ateam/cli.py                 click group and commands
src/ateam/paths.py               TargetPaths, repo_id(), home_dir()
src/ateam/config.py              TargetConfig model, load_config()
src/ateam/globs.py               matches(), expand(), normalise checks
src/ateam/gitops.py              GitRepo wrapper around subprocess git
src/ateam/records/model.py       Record and nested models, compute_fingerprint()
src/ateam/records/store.py       load_records(), save_record(), next_id(), RecordStoreError
src/ateam/records/graph.py       RecordGraph queries
src/ateam/records/validate.py    Problem, validate_records()
src/ateam/records/lifecycle.py   approve(), reject(), supersede(), retire(), LifecycleError
src/ateam/records/service.py     RecordService: guards + save + git commit
src/ateam/contracts/model.py     Contract and nested models
src/ateam/contracts/store.py     load_contracts(), save_contract(), next_id()
src/ateam/contracts/select.py    select_records(), freeze()
src/ateam/state/db.py            StateDB (SQLite), transition tables, InvalidTransition
src/ateam/checkpoint.py          Checkpoint, load_handoff()
src/ateam/roles.py               Role, RoleSpec, ROLE_SPECS, report schemas
src/ateam/tiers.py               TierSpec, load_tiers(), default tiers
src/ateam/brief.py               Brief, assemble_brief(), render_brief()
src/ateam/runner/policy.py       Policy: can_use_tool, pre_tool_use, hook_matchers()
src/ateam/runner/options.py      clean_env(), worker_options()
src/ateam/runner/run.py          WorkerResult, run_worker()
src/ateam/treecheck.py           TreeState, tree_state(), ScopeResult, scope_check()
tests/conftest.py                tmp target repo fixture, tmp home fixture
tests/test_<module>.py           one test file per module above
tests/live/test_worker_live.py   opt-in end-to-end run
```

---

### Task 1: Package scaffold and CLI entry point

**Files:**
- Create: `pyproject.toml`, `src/ateam/__init__.py`, `src/ateam/cli.py`, `tests/conftest.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `ateam.__version__ = "0.1.0"`, `ateam.SPIKED_CLI_VERSION = "2.1.310"`; `ateam.cli.main` (click group) registered as console script `ateam`; `tests/conftest.py` fixtures `target_repo(tmp_path) -> Path` (a git repo on branch `main` with one commit, `user.name`/`user.email` set, containing `src/app.py`, `tests/test_app.py`, and an empty `.ateam/records/` and `.ateam/contracts/`) and `ateam_home(tmp_path, monkeypatch) -> Path` (sets env `ATEAM_HOME`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from click.testing import CliRunner
from ateam.cli import main

def test_version_flag():
    r = CliRunner().invoke(main, ["--version"])
    assert r.exit_code == 0
    assert "0.1.0" in r.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL with "No module named 'ateam'"

- [ ] **Step 3: Create `pyproject.toml` (setuptools, src layout, `[project.scripts] ateam = "ateam.cli:main"`, dependencies: click, pydantic>=2, pyyaml, claude-agent-sdk>=0.2.174,<0.3; optional `dev`: pytest, pytest-asyncio), `src/ateam/__init__.py`, and `src/ateam/cli.py` with `@click.group() @click.version_option(__version__) def main(): ...`**

Install editable: `.venv/bin/pip install -e '.[dev]'`

- [ ] **Step 4: Write `tests/conftest.py` fixtures `target_repo` and `ateam_home` as specified in Interfaces**

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/ateam tests
git commit -m "feat: ateam package scaffold and CLI entry point"
```

---

### Task 2: Target paths and config

**Files:**
- Create: `src/ateam/paths.py`, `src/ateam/config.py`, `tests/test_paths.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `paths.TargetPaths` dataclass: `root, ateam, records, contracts, plans, reports, config_file` (all `Path`); `TargetPaths.from_root(root: Path) -> TargetPaths`; `TargetPaths.exists() -> bool` (true when `root/.ateam` is a directory).
  - `paths.repo_id(root: Path) -> str`: first 12 hex chars of sha1 of `str(root.resolve())`.
  - `paths.home_dir(root: Path) -> Path`: `$ATEAM_HOME/<repo_id>` or `~/.ateam/<repo_id>`, created on call.
  - `config.TargetConfig` pydantic model with defaults: `target_branch: str = "main"`, `setup: list[str] = []`, `env: dict[str,str] = {}`, `checks: list[NamedCheck] = []` (`NamedCheck(name: str, cmd: str)`), `serialize_checks: bool = False`, `protected_paths: list[str] = [".ateam/**", ".claude/**"]`, `max_workers: int = 2`, `global_record_cap: int = 5`, `tiers: dict[str, dict] = {}`.
  - `config.load_config(paths: TargetPaths) -> TargetConfig`: reads `config.yaml`, returns defaults when the file is absent.
  - `config.save_config(paths, cfg) -> Path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_paths.py
def test_from_root_layout(tmp_path):
    p = TargetPaths.from_root(tmp_path)
    assert p.records == tmp_path / ".ateam" / "records"
    assert p.config_file == tmp_path / ".ateam" / "config.yaml"
    assert p.exists() is False

def test_repo_id_stable_and_short(tmp_path):
    assert repo_id(tmp_path) == repo_id(tmp_path) and len(repo_id(tmp_path)) == 12

def test_home_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATEAM_HOME", str(tmp_path / "h"))
    assert home_dir(tmp_path).parent == tmp_path / "h" and home_dir(tmp_path).is_dir()

# tests/test_config.py
def test_defaults_when_missing(target_repo):
    cfg = load_config(TargetPaths.from_root(target_repo))
    assert cfg.target_branch == "main" and cfg.max_workers == 2 and cfg.protected_paths == [".ateam/**", ".claude/**"]

def test_roundtrip(target_repo):
    p = TargetPaths.from_root(target_repo)
    save_config(p, TargetConfig(target_branch="develop", checks=[NamedCheck(name="unit", cmd="pytest")], serialize_checks=True))
    cfg = load_config(p)
    assert cfg.target_branch == "develop" and cfg.checks[0].cmd == "pytest" and cfg.serialize_checks
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_paths.py tests/test_config.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `paths.py` and `config.py` per Interfaces**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_paths.py tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/paths.py src/ateam/config.py tests/test_paths.py tests/test_config.py
git commit -m "feat: target paths and config model"
```

---

### Task 3: Record model and store

**Files:**
- Create: `src/ateam/globs.py`, `src/ateam/records/__init__.py`, `src/ateam/records/model.py`, `src/ateam/records/store.py`, `tests/test_globs.py`, `tests/test_record_model.py`, `tests/test_record_store.py`

**Interfaces:**
- Produces:
  - `globs.matches(pattern: str, path: str) -> bool` using `glob.translate(pattern, recursive=True, include_hidden=True)`; `globs.expand(patterns: list[str], root: Path) -> list[str]` (sorted, relative, files only); `globs.is_valid_pattern(p: str) -> bool` (false for absolute, `./` prefix, trailing `/`, or any `..` segment).
  - `records.model`: `StrEnum`s `Kind(decision, invariant, convention)`, `Status(proposed, approved, rejected, superseded, retired)`, `Origin(documented, inferred)`, `Basis(requirement, observation)`; models `Evidence(type: Literal["doc","claude-md","agents-md","commit-message","code","test","commit"], ref: str, note: str|None=None)`, `Binds(paths: list[str]=[], incidental: list[str]=[])`, `HistoryEntry(at: datetime, by: str, to: Status, why: str|None=None)`, `Record(id, kind, status, origin, basis, statement: str, rationale: str|None=None, evidence: list[Evidence]=[], binds: Binds=Binds(), supersedes: list[str]=[], serves: list[str]=[], fingerprint: str, history: list[HistoryEntry]=[])` with `Record.is_global() -> bool` (no `binds.paths`), `Record.content_hash() -> str` (sha256 of `model_dump_json(exclude={"history"})` with sorted keys), `Record.slug() -> str` (first six words of `statement`, lowercase, non-alphanumerics to `-`, max 40 chars).
  - `records.model.compute_fingerprint(evidence: list[Evidence]) -> str`: sha256 of newline-joined sorted `f"{e.type}:{e.ref}"`.
  - `records.store.RecordStoreError(Exception)`; `load_records(records_dir: Path) -> dict[str, Record]` (raises on id/filename mismatch or duplicate id, naming the paths); `save_record(records_dir, record) -> Path` (filename `f"{record.id}-{record.slug()}.yaml"`, removes an older file for the same id with a different slug); `next_id(records_dir, prefix="R") -> str` (max existing + 1, zero-padded to 4, starting at `R-0001`); `file_hash(path) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_globs.py
def test_matches_double_star():
    assert matches("src/api/**", "src/api/books/routes.py")
    assert not matches("src/api/**", "src/data/books.py")
def test_invalid_patterns():
    assert all(not is_valid_pattern(p) for p in ["/abs/x", "./src/**", "src/", "src/../x"])
    assert is_valid_pattern("src/**/*.py")

# tests/test_record_model.py
def test_fingerprint_is_order_independent():
    a = [Evidence(type="doc", ref="d.md"), Evidence(type="test", ref="t.py::x")]
    assert compute_fingerprint(a) == compute_fingerprint(list(reversed(a)))
def test_content_hash_ignores_history(sample_record):
    h = sample_record.content_hash()
    sample_record.history.append(HistoryEntry(at=datetime.now(UTC), by="owner", to=Status.approved))
    assert sample_record.content_hash() == h
def test_slug():
    assert Record(..., statement="Reading state is only ever set by the owner").slug() == "reading-state-is-only-ever-set"

# tests/test_record_store.py
def test_roundtrip_and_filename(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    p = save_record(d, sample_record)
    assert p.name == f"{sample_record.id}-{sample_record.slug()}.yaml"
    assert load_records(d)[sample_record.id] == sample_record
def test_next_id(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    assert next_id(d) == "R-0001"
    save_record(d, sample_record.model_copy(update={"id": "R-0007"}))
    assert next_id(d) == "R-0008"
def test_id_filename_mismatch_raises(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    (d / "R-0002-wrong.yaml").write_text(yaml.safe_dump(sample_record.model_dump(mode="json")))
    with pytest.raises(RecordStoreError, match="R-0002-wrong.yaml"):
        load_records(d)
def test_duplicate_id_raises(target_repo, sample_record):
    d = target_repo / ".ateam/records"
    save_record(d, sample_record); (d / f"{sample_record.id}-copy.yaml").write_text((d / f"{sample_record.id}-{sample_record.slug()}.yaml").read_text())
    with pytest.raises(RecordStoreError, match="duplicate"):
        load_records(d)
```

Add a `sample_record` fixture to `tests/conftest.py`: `R-0001`, kind invariant, status proposed, origin documented, basis requirement, statement "Reading state is only ever set by the owner", one `doc` evidence `docs/spec.md#anti-goals`, binds `["src/data/books.py", "src/api/books/**"]`, serves `["owner controls their own data"]`, fingerprint computed, one history entry by `ateam-init` to proposed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_globs.py tests/test_record_model.py tests/test_record_store.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `globs.py`, `records/model.py`, `records/store.py` per Interfaces**

YAML output uses `yaml.safe_dump(record.model_dump(mode="json"), sort_keys=False)`; datetimes serialise as ISO strings.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_globs.py tests/test_record_model.py tests/test_record_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/globs.py src/ateam/records tests/test_globs.py tests/test_record_model.py tests/test_record_store.py tests/conftest.py
git commit -m "feat: record model, fingerprint and YAML store"
```

---

### Task 4: Record graph and validator

**Files:**
- Create: `src/ateam/records/graph.py`, `src/ateam/records/validate.py`, `tests/test_record_graph.py`, `tests/test_record_validate.py`

**Interfaces:**
- Consumes: `Record`, `Status`, `globs.matches`, `globs.is_valid_pattern`, `globs.expand`.
- Produces:
  - `graph.RecordGraph(records: dict[str, Record])` with `binding(path: str, approved_only: bool = True) -> list[Record]` (records whose any `binds.paths` glob matches `path`, sorted by id), `globals(approved_only=True) -> list[Record]`, `chain(record_id: str) -> list[str]` (ids reachable through `supersedes`, depth-first, excluding self), `cycles() -> list[list[str]]`, `dangling() -> list[tuple[str, str]]` (`(from_id, missing_id)` over `supersedes`), `superseders(record_id) -> list[str]` (ids whose `supersedes` contains it).
  - `validate.Problem` dataclass `(level: Literal["error","warning"], record_id: str|None, message: str)`; `validate.validate_records(records: dict[str, Record], repo_root: Path, known_hashes: dict[str, str] | None = None) -> list[Problem]`.

Validator rules, in order: invalid glob pattern (error); `origin=documented` without evidence of type `doc`, `claude-md`, `agents-md` or `commit-message` (error); dangling `supersedes` (error); cycle (error); an approved record whose `supersedes` target is still `approved` or `proposed` (error, "supersession incomplete"); a `superseded` record with no superseder (error); glob matching no file under `repo_root` (warning, "dead glob"); a `serves` phrase appearing in more than one record (warning, listing ids); `known_hashes` given and a record's `content_hash()` differs from the stored hash (warning, "hand edit").

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_record_graph.py
def test_binding_and_globals(records_three):   # fixture: R-0001 binds src/api/**; R-0002 global; R-0003 proposed binds src/**
    g = RecordGraph(records_three)
    assert [r.id for r in g.binding("src/api/x.py")] == ["R-0001"]
    assert [r.id for r in g.binding("src/api/x.py", approved_only=False)] == ["R-0001", "R-0003"]
    assert [r.id for r in g.globals()] == ["R-0002"]
def test_chain_cycles_dangling(records_three):
    records_three["R-0001"].supersedes = ["R-0002"]; records_three["R-0002"].supersedes = ["R-0001", "R-0099"]
    g = RecordGraph(records_three)
    assert g.chain("R-0001") == ["R-0002", "R-0001"] or set(g.chain("R-0001")) == {"R-0002", "R-0001"}
    assert g.cycles() and g.dangling() == [("R-0002", "R-0099")]

# tests/test_record_validate.py
def test_clean_set_has_no_errors(target_repo, records_three):
    assert [p for p in validate_records(records_three, target_repo) if p.level == "error"] == []
def test_invalid_glob_is_error(target_repo, records_three):
    records_three["R-0001"].binds.paths = ["./src/**"]
    assert any("invalid glob" in p.message and p.level == "error" for p in validate_records(records_three, target_repo))
def test_documented_needs_doc_evidence(target_repo, records_three): ...
def test_supersession_incomplete(target_repo, records_three): ...
def test_dead_glob_is_warning(target_repo, records_three): ...
def test_duplicate_serves_is_warning(target_repo, records_three): ...
def test_hand_edit_detected(target_repo, records_three):
    hashes = {rid: r.content_hash() for rid, r in records_three.items()}
    records_three["R-0001"].statement += " changed"
    assert any("hand edit" in p.message for p in validate_records(records_three, target_repo, hashes))
```

`records_three` fixture goes in `tests/conftest.py`; `target_repo` must contain `src/api/x.py` so the `src/api/**` glob is live.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_record_graph.py tests/test_record_validate.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `graph.py` and `validate.py` per Interfaces, plain dictionaries, no networkx**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_record_graph.py tests/test_record_validate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/records/graph.py src/ateam/records/validate.py tests/test_record_graph.py tests/test_record_validate.py tests/conftest.py
git commit -m "feat: record graph queries and validator"
```

---

### Task 5: Git operations and record lifecycle with commits

**Files:**
- Create: `src/ateam/gitops.py`, `src/ateam/records/lifecycle.py`, `src/ateam/records/service.py`, `tests/test_gitops.py`, `tests/test_record_lifecycle.py`, `tests/test_record_service.py`

**Interfaces:**
- Consumes: `Record`, `Status`, `Basis`, store functions, `TargetPaths`, `TargetConfig`.
- Produces:
  - `gitops.GitError(Exception)`; `gitops.GitRepo(root: Path)` with `discover(start: Path) -> GitRepo` (classmethod via `rev-parse --show-toplevel`), `run(*args: str) -> str` (stdout, `GitError` on non-zero), `current_branch() -> str`, `head() -> str`, `is_clean(paths: list[str] | None = None) -> bool`, `status_porcelain() -> list[tuple[str, str]]` (`(xy, path)`, untracked included, never stripped), `diff_names(base: str) -> list[str]`, `commit_paths(paths: list[str], message: str) -> str` (stages only `paths`, commits, returns sha; `GitError` if nothing staged), `worktree_add(path: Path, branch: str, base: str) -> None`, `worktree_remove(path: Path, force: bool = False) -> None`, `exclude_in_worktree(path: Path, patterns: list[str]) -> None` (appends to `<git-dir>/info/exclude` of that worktree).
  - `lifecycle.LifecycleError(Exception)`; pure functions returning new `Record`s with a `HistoryEntry` appended: `approve(record, by="owner", basis: Basis | None = None, at=None)` (proposed only), `reject(record, reason: str, by="owner", at=None)` (proposed only), `supersede(old: Record, new: Record, by="owner", at=None) -> tuple[Record, Record]` (old approved, new proposed; new gets `old.id` added to `supersedes` and becomes approved; old becomes superseded), `retire(record, reason, by="owner", at=None)` (approved only).
  - `service.RecordService(paths: TargetPaths, git: GitRepo, config: TargetConfig)` with `approve(record_id, basis=None) -> Record`, `reject(record_id, reason) -> Record`, `supersede(old_id, new_id) -> tuple[Record, Record]`, `retire(record_id, reason) -> Record`, each: guard (`git.current_branch() == config.target_branch` else `LifecycleError("checkout is on <b>, expected <target>")`; `git.is_clean([".ateam"])` else `LifecycleError(".ateam/ has uncommitted changes")`), apply the pure transition, `save_record`, `git.commit_paths([".ateam/records"], f"ateam: {verb} {ids}")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gitops.py
def test_branch_head_clean(target_repo):
    g = GitRepo.discover(target_repo)
    assert g.current_branch() == "main" and len(g.head()) == 40 and g.is_clean()
def test_porcelain_keeps_leading_space(target_repo):
    (target_repo / "src/app.py").write_text("changed\n")
    assert (" M", "src/app.py") in GitRepo(target_repo).status_porcelain()
def test_commit_paths_only_stages_given_paths(target_repo):
    g = GitRepo(target_repo)
    (target_repo / ".ateam/records/x.yaml").write_text("id: x\n"); (target_repo / "src/app.py").write_text("changed\n")
    g.commit_paths([".ateam/records"], "ateam: test")
    assert not g.is_clean(["src"]) and g.is_clean([".ateam"])
def test_worktree_add_remove_and_exclude(target_repo, tmp_path): ...

# tests/test_record_lifecycle.py
def test_approve_sets_status_basis_history(sample_record):
    r = approve(sample_record, basis=Basis.requirement)
    assert r.status == Status.approved and r.history[-1].to == Status.approved and r.history[-1].by == "owner"
def test_approve_rejects_non_proposed(sample_record):
    with pytest.raises(LifecycleError): approve(approve(sample_record))
def test_supersede_is_one_transition(sample_record):
    old = approve(sample_record); new = sample_record.model_copy(update={"id": "R-0002"})
    old2, new2 = supersede(old, new)
    assert old2.status == Status.superseded and new2.status == Status.approved and "R-0001" in new2.supersedes
def test_retire_requires_approved(sample_record): ...

# tests/test_record_service.py
def test_approve_commits_only_ateam(record_service, target_repo, sample_record):
    save_record(target_repo / ".ateam/records", sample_record); GitRepo(target_repo).commit_paths([".ateam"], "seed")
    (target_repo / "src/app.py").write_text("dirty\n")
    r = record_service.approve("R-0001")
    assert r.status == Status.approved and "ateam: approve R-0001" in GitRepo(target_repo).run("log", "-1", "--format=%s")
    assert not GitRepo(target_repo).is_clean(["src"])
def test_refuses_on_other_branch(record_service, target_repo):
    GitRepo(target_repo).run("checkout", "-q", "-b", "other")
    with pytest.raises(LifecycleError, match="other"): record_service.approve("R-0001")
def test_refuses_when_ateam_dirty(record_service, target_repo, sample_record): ...
```

`record_service` fixture builds `RecordService(TargetPaths.from_root(target_repo), GitRepo(target_repo), TargetConfig())`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_gitops.py tests/test_record_lifecycle.py tests/test_record_service.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `gitops.py`, `records/lifecycle.py`, `records/service.py` per Interfaces**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_gitops.py tests/test_record_lifecycle.py tests/test_record_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/gitops.py src/ateam/records/lifecycle.py src/ateam/records/service.py tests/test_gitops.py tests/test_record_lifecycle.py tests/test_record_service.py tests/conftest.py
git commit -m "feat: git operations and record lifecycle with guarded commits"
```

---

### Task 6: Contract model, store, selection and freezing

**Files:**
- Create: `src/ateam/contracts/__init__.py`, `src/ateam/contracts/model.py`, `src/ateam/contracts/store.py`, `src/ateam/contracts/select.py`, `tests/test_contract_model.py`, `tests/test_contract_store.py`, `tests/test_contract_select.py`

**Interfaces:**
- Consumes: `Record`, `RecordGraph`, `globs`.
- Produces:
  - `contracts.model`: `ContractStatus` StrEnum (values in Global Constraints); `Level(low, medium, high)`; `Check(type: Literal["test","command","judgment"], ref: str | None = None)`; `Acceptance(id: str, statement: str, check: Check)`; `NamedCheck` re-exported from `config`; `Allocation(complexity: Level, risk: Level, uncertainty: Level, reasoning: str, tier: dict[str, str])` (keys `build|test|investigate`, values tier name or `"none"`); `Budget(attempts: int = 3, usd_per_attempt: float = 2.0, max_turns: int = 60)`; `RecordRef(id: str, hash: str)`; `Contract(id, plan: str, title, status: ContractStatus = draft, depends_on: list[str] = [], paths: list[str], incidental: list[str] = [], records: list[RecordRef] = [], acceptance: list[Acceptance], checks: list[NamedCheck] = [], allocation: Allocation, budget: Budget = Budget())` with `content_hash() -> str` (sha256 over `model_dump_json(exclude={"status"})`, sorted keys) and `slug()` from `title`.
  - `contracts.store`: `ContractStoreError`, `load_contracts(dir) -> dict[str, Contract]`, `save_contract(dir, contract) -> Path`, `next_id(dir, prefix="C") -> str`, same conventions as records.
  - `contracts.select.select_records(paths: list[str], graph: RecordGraph, repo_root: Path) -> list[Record]`: approved records that are global, or whose glob matches any file produced by `globs.expand(paths, repo_root)`, or whose glob string equals a contract path string, or where one glob is the other with a trailing `/**` removed (prefix directory). Sorted by id, no duplicates.
  - `contracts.select.freeze(contract: Contract, records: list[Record]) -> Contract`: returns a copy with `records = [RecordRef(id, r.content_hash())]` and `status = ready` if `depends_on` is empty else `draft`.
  - `contracts.select.drift(contract: Contract, records: dict[str, Record]) -> list[str]`: ids whose current `content_hash()` differs from the frozen ref, or that are missing or no longer approved.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_contract_model.py
def test_content_hash_ignores_status(sample_contract):
    h = sample_contract.content_hash(); assert sample_contract.model_copy(update={"status": ContractStatus.building}).content_hash() == h
def test_allocation_tier_keys(sample_contract):
    assert set(sample_contract.allocation.tier) == {"build", "test", "investigate"}

# tests/test_contract_store.py
def test_roundtrip_and_next_id(target_repo, sample_contract): ...   # mirrors record store tests

# tests/test_contract_select.py
def test_select_by_file_overlap_and_globals(target_repo, records_three):   # R-0001 binds src/api/**, R-0002 global; target has src/api/x.py
    g = RecordGraph({k: approve(v) if v.status == Status.proposed else v for k, v in records_three.items()})
    assert [r.id for r in select_records(["src/api/x.py"], g, target_repo)] == ["R-0001", "R-0002", "R-0003"]
def test_select_by_glob_prefix_for_new_files(target_repo, records_three):
    g = RecordGraph(records_three)
    assert "R-0001" in [r.id for r in select_records(["src/api/new/**"], g, target_repo)]
def test_freeze_and_drift(sample_contract, records_three):
    frozen = freeze(sample_contract, [records_three["R-0001"]])
    assert frozen.status == ContractStatus.ready and frozen.records[0].id == "R-0001"
    records_three["R-0001"].statement += " x"
    assert drift(frozen, records_three) == ["R-0001"]
```

`sample_contract` fixture in `conftest.py`: `C-0001`, plan `P-0001`, title "Expose reading state through the books API", paths `["src/api/**", "tests/api/test_books.py"]`, incidental `["requirements.txt"]`, two acceptance criteria (one `test` check `tests/api/test_books.py::test_reading_state_present`, one `judgment`), allocation medium/high/low with tier `{"build": "standard", "test": "careful", "investigate": "none"}`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_contract_model.py tests/test_contract_store.py tests/test_contract_select.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `contracts/model.py`, `contracts/store.py`, `contracts/select.py` per Interfaces**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_contract_model.py tests/test_contract_store.py tests/test_contract_select.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/contracts tests/test_contract_model.py tests/test_contract_store.py tests/test_contract_select.py tests/conftest.py
git commit -m "feat: contract model, store, record selection and freezing"
```

---

### Task 7: SQLite state store

**Files:**
- Create: `src/ateam/state/__init__.py`, `src/ateam/state/db.py`, `tests/test_state_db.py`

**Interfaces:**
- Consumes: nothing from other tasks (statuses are strings validated against the tables below).
- Produces: `state.db.InvalidTransition(Exception)`; `state.db.FEATURE_TRANSITIONS` and `CONTRACT_TRANSITIONS` as `dict[str, set[str]]` encoding the design's state machines (feature: `requested->investigating`, `investigating->plan_proposed`, `plan_proposed->plan_approved|plan_rejected`, `plan_approved->executing`, `executing->paused|integrating`, `paused->executing|abandoned`, `integrating->merge_proposed`, `merge_proposed->merged|plan_rejected`; contract: `draft->ready`, `ready->building`, `building->verifying|failed|paused`, `verifying->verified|failed|paused`, `failed->building|paused`, `paused->building|ready`, `verified->integrated`); `state.db.StateDB(path: Path)` with `init_schema()` (idempotent, `schema_version` table set to 1), `set_record_hash(record_id, hash)`, `record_hashes() -> dict[str, str]`, `upsert_contract(contract_id, plan_id, status, content_hash)`, `contract_status(contract_id) -> str | None`, `transition(kind: Literal["feature","contract"], id: str, to: str, by: str, reason: str | None = None) -> None` (inserts into `transitions(kind, id, from_status, to_status, by, reason, at)` and updates the current status; `InvalidTransition` if not allowed; a feature's first transition may start from `None` only to `requested`), `transitions(kind, id) -> list[dict]`, `start_attempt(contract_id, role, tier, model, predicted: dict[str, str]) -> int`, `finish_attempt(attempt_id, outcome: str, failure_class: str | None, tokens_in: int, tokens_out: int, cost_usd: float, turns: int, wall_s: float, session_id: str | None) -> None`, `telemetry_rows() -> list[dict]`, `set_checkpoint(contract_id, attempt_id, facts: dict) -> None`, `latest_checkpoint(contract_id) -> dict | None`, `acquire_run_lock(plan_id) -> bool`, `release_run_lock(plan_id) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_init_schema_idempotent(tmp_path):
    db = StateDB(tmp_path / "s.db"); db.init_schema(); db.init_schema()
def test_contract_transition_rules(tmp_path):
    db = StateDB(tmp_path / "s.db"); db.init_schema(); db.upsert_contract("C-0001", "P-0001", "draft", "h")
    db.transition("contract", "C-0001", "ready", by="ateam")
    with pytest.raises(InvalidTransition): db.transition("contract", "C-0001", "integrated", by="ateam")
    assert db.contract_status("C-0001") == "ready" and db.transitions("contract", "C-0001")[-1]["to_status"] == "ready"
def test_feature_starts_at_requested(tmp_path):
    db = StateDB(tmp_path / "s.db"); db.init_schema()
    with pytest.raises(InvalidTransition): db.transition("feature", "P-0001", "executing", by="ateam")
    db.transition("feature", "P-0001", "requested", by="owner")
def test_attempt_and_telemetry(tmp_path):
    db = StateDB(tmp_path / "s.db"); db.init_schema()
    a = db.start_attempt("C-0001", "build", "standard", "sonnet", {"complexity": "medium", "risk": "high", "uncertainty": "low"})
    db.finish_attempt(a, "verified", None, 1000, 200, 0.05, 12, 30.5, "sess")
    row = db.telemetry_rows()[0]
    assert row["predicted_risk"] == "high" and row["cost_usd"] == 0.05 and row["outcome"] == "verified"
def test_checkpoint_and_lock(tmp_path):
    db = StateDB(tmp_path / "s.db"); db.init_schema()
    db.set_checkpoint("C-0001", 1, {"head": "abc"}); assert db.latest_checkpoint("C-0001") == {"head": "abc"}
    assert db.acquire_run_lock("P-0001") and not db.acquire_run_lock("P-0002"); db.release_run_lock("P-0001"); assert db.acquire_run_lock("P-0002")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_state_db.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `state/db.py` with `sqlite3`, one connection per `StateDB`, `PRAGMA journal_mode=WAL`, every write in a transaction; `checkpoints.facts` stored as JSON text; the run lock is a single-row table `run_lock(plan_id, acquired_at)`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_state_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/state tests/test_state_db.py
git commit -m "feat: SQLite state store with state machines, attempts and telemetry"
```

---

### Task 8: CLI commands for records, contracts, status and doctor

**Files:**
- Modify: `src/ateam/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 2 to 7.
- Produces, all accepting `--target PATH` (default cwd) and resolving `TargetPaths`, `GitRepo.discover`, `load_config`, `StateDB(home_dir(root)/"state.db")`:
  - `ateam records validate` (prints problems as `ERROR R-0001: ...` / `WARN ...`; exit 1 if any error), `ateam records list [--status S]`, `ateam records show R-0001`, `ateam records approve R-0001 [--basis requirement|observation]` (before approving, prints every approved record whose globs overlap the candidate's, headed `Overlapping approved records:`), `ateam records reject R-0001 "reason"`, `ateam records supersede R-0001 --with R-0002`, `ateam records retire R-0001 "reason"`; each lifecycle command stores the new `content_hash` with `set_record_hash` after the commit.
  - `ateam contracts show C-0001`, `ateam contracts freeze C-0001` (select + freeze + save + `upsert_contract` + `set_record_hash` for each selected record; prints the selected record ids).
  - `ateam status` (feature and contract statuses from the DB, latest attempt per contract; prints `No .ateam/ directory. Run: ateam init` and exits 2 when `TargetPaths.exists()` is false).
  - `ateam doctor` (prints: ATeam version, SDK version from `claude_agent_sdk.__version__`, bundled CLI version from `claude_agent_sdk._cli_version.__cli_version__`, `SPIKED_CLI_VERSION`, a `WARN` line if they differ, target branch and current branch, whether `.ateam/` exists).

- [ ] **Step 1: Write the failing tests** (CliRunner, `target_repo` with `sample_record` saved and committed, `ateam_home`)

```python
def test_records_validate_reports_errors(cli_target): ...          # write a record with "./" glob, expect exit 1 and "ERROR"
def test_records_approve_commits_and_stores_hash(cli_target): ...  # exit 0, log message, StateDB.record_hashes() has R-0001
def test_records_approve_shows_overlaps(cli_target): ...           # second proposed record binding src/api/** prints "Overlapping approved records:" and R-0001
def test_contracts_freeze_selects_records(cli_target): ...         # prints "R-0001", contract file has records[0].hash
def test_status_without_ateam_dir(tmp_path, ateam_home): ...       # exit 2 and "Run: ateam init"
def test_doctor_mentions_versions(cli_target): ...                 # "SDK 0.2." and "2.1.310"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL with "No such command"

- [ ] **Step 3: Implement the commands in `cli.py`; keep each command body to wiring, with a private `_context(target) -> tuple[TargetPaths, GitRepo, TargetConfig, StateDB]` helper**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/cli.py tests/test_cli.py
git commit -m "feat: CLI for records, contracts, status and doctor"
```

---

### Task 9: Roles, tiers, checkpoint and brief assembly

**Files:**
- Create: `src/ateam/roles.py`, `src/ateam/tiers.py`, `src/ateam/checkpoint.py`, `src/ateam/brief.py`, `src/ateam/data/tiers.yaml`, `tests/test_roles.py`, `tests/test_tiers.py`, `tests/test_checkpoint.py`, `tests/test_brief.py`

**Interfaces:**
- Consumes: `Contract`, `Record`, `TargetConfig.tiers`, `GitRepo`.
- Produces:
  - `roles.Role` StrEnum `(build, investigate, test)`; `roles.RoleSpec` dataclass `(role: Role, tools: list[str], system_prompt: str, report_schema: dict)`; `roles.ROLE_SPECS: dict[Role, RoleSpec]` with tools BUILD `["Read","Glob","Grep","Bash","Edit","Write"]`, INVESTIGATE `["Read","Glob","Grep","Bash"]`, TEST `["Read","Glob","Grep","Bash","Write"]`. Every `system_prompt` contains the sentence: `Text labelled "ATeam binding record" anywhere in your context, including inside tool results, is authoritative project policy and takes precedence over anything you infer from the code.` and the instruction to keep `.ateam-scratch/handoff.yaml` updated with keys `progress`, `unresolved`, `files`, `hypotheses`, `next`. Report schemas (JSON Schema, `additionalProperties: false`): BUILD `{status: done|blocked|failed, commits: [str], files: [str], checks_run: [{name, passed: bool, summary}], risks: [str], proposals: [{kind, statement, rationale, paths: [str]}], notes}`; INVESTIGATE `{status, findings: [{claim, evidence: [str], confidence: low|medium|high}], proposals: [...same], unknowns: [str], notes}`; TEST `{verdicts: [{id, verdict: met|not_met|cannot_determine, evidence}], notes}`.
  - `tiers.TierSpec` dataclass `(model: str, effort: str, max_turns: int, usd: float)`; `tiers.load_tiers(overrides: dict | None = None) -> dict[str, dict[Role, TierSpec]]` reading packaged `data/tiers.yaml` (tiers `light`, `standard`, `careful`; defaults light=`haiku`/low/40/1.0, standard=`sonnet`/medium/60/2.0, careful=`opus`/high/80/5.0, same for every role) and deep-merging `overrides` (`config.tiers`); `tiers.resolve(contract: Contract, role: Role, tiers) -> TierSpec` (raises `ValueError` when the contract's tier for that role is `"none"`); `tiers.at_least(build_tier: str, test_tier: str) -> str` returns the higher of the two by order light < standard < careful.
  - `checkpoint.Checkpoint` dataclass `(base: str, head: str, commits: list[str], diff_names: list[str], dirty: list[tuple[str, str]], checks: list[dict], contract_hash: str, record_hashes: dict[str, str], session_id: str | None, notes: dict | None, warnings: list[str])`; `checkpoint.load_handoff(worktree: Path) -> tuple[dict | None, list[str]]` (reads `.ateam-scratch/handoff.yaml`; returns `(None, [warning])` when missing, empty, invalid YAML or not a mapping); `checkpoint.collect(git: GitRepo, worktree: Path, base: str, contract: Contract, records: dict[str, Record], session_id, checks) -> Checkpoint`.
  - `brief.Brief` dataclass `(binding: str, task: str, prior_work: str, notes: str | None)`; `brief.assemble_brief(contract: Contract, records: list[Record], checkpoint: Checkpoint | None, role: Role) -> Brief` (TEST always gets `notes=None`; records rendered as `### ATeam binding record R-0001 (invariant)` blocks with statement, rationale, binds; task section renders paths, incidental, acceptance criteria with ids, checks, budget); `brief.render_brief(b: Brief) -> str` with headings `## Binding`, `## Task`, `## Verified prior work`, `## Previous worker's notes (unverified)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_roles.py
def test_role_tools_and_authority_sentence():
    assert "Edit" not in ROLE_SPECS[Role.investigate].tools and "Edit" not in ROLE_SPECS[Role.test].tools
    assert all('"ATeam binding record"' in s.system_prompt for s in ROLE_SPECS.values())
def test_report_schemas_are_strict():
    assert all(s.report_schema["additionalProperties"] is False for s in ROLE_SPECS.values())

# tests/test_tiers.py
def test_defaults_and_override():
    t = load_tiers({"standard": {"build": {"model": "opus"}}})
    assert t["standard"][Role.build].model == "opus" and t["standard"][Role.test].model == "sonnet"
def test_resolve_none_raises(sample_contract):
    with pytest.raises(ValueError): resolve(sample_contract, Role.investigate, load_tiers())
def test_at_least(): assert at_least("standard", "light") == "standard"

# tests/test_checkpoint.py
def test_load_handoff_variants(tmp_path):
    (tmp_path / ".ateam-scratch").mkdir()
    assert load_handoff(tmp_path)[0] is None
    (tmp_path / ".ateam-scratch/handoff.yaml").write_text(": not yaml ["); assert load_handoff(tmp_path)[0] is None and load_handoff(tmp_path)[1]
    (tmp_path / ".ateam-scratch/handoff.yaml").write_text("- a list"); assert load_handoff(tmp_path)[0] is None
    (tmp_path / ".ateam-scratch/handoff.yaml").write_text("progress: half\nnext: tests\n"); assert load_handoff(tmp_path)[0]["next"] == "tests"
def test_collect_facts(target_repo, sample_contract, records_three): ...  # make a commit in target_repo, assert commits and diff_names

# tests/test_brief.py
def test_sections_and_test_role_omits_notes(sample_contract, records_three):
    cp = Checkpoint(base="a", head="b", commits=[], diff_names=[], dirty=[], checks=[], contract_hash="h", record_hashes={}, session_id=None, notes={"next": "x"}, warnings=[])
    b = assemble_brief(sample_contract, [records_three["R-0001"]], cp, Role.build)
    assert "ATeam binding record R-0001" in b.binding and "A1" in b.task and b.notes and "x" in b.notes
    assert assemble_brief(sample_contract, [], cp, Role.test).notes is None
    assert "## Previous worker's notes (unverified)" in render_brief(b)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_roles.py tests/test_tiers.py tests/test_checkpoint.py tests/test_brief.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement the four modules and `data/tiers.yaml` per Interfaces (add `package-data` for `ateam/data/*.yaml` in `pyproject.toml`)**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_roles.py tests/test_tiers.py tests/test_checkpoint.py tests/test_brief.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/roles.py src/ateam/tiers.py src/ateam/checkpoint.py src/ateam/brief.py src/ateam/data pyproject.toml tests/test_roles.py tests/test_tiers.py tests/test_checkpoint.py tests/test_brief.py
git commit -m "feat: roles, tiers, checkpoint and brief assembly"
```

---

### Task 10: Worker policy and clean-room options

**Files:**
- Create: `src/ateam/runner/__init__.py`, `src/ateam/runner/policy.py`, `src/ateam/runner/options.py`, `tests/test_policy.py`, `tests/test_options.py`

**Interfaces:**
- Consumes: `Role`, `RoleSpec`, `Record`, `globs.matches`, SDK types `PermissionResultAllow`, `PermissionResultDeny`, `HookMatcher`, `ClaudeAgentOptions`.
- Produces:
  - `policy.DESTRUCTIVE_PATTERNS: list[str]` regexes, at least: `\bgit\s+push\b`, `\bgit\s+merge\b`, `\bgit\s+rebase\b`, `\bgit\s+reset\s+--hard\b`, `\bgit\s+checkout\s+\.`, `\brm\s+-[a-z]*r[a-z]*f\b|\brm\s+-[a-z]*f[a-z]*r\b`, `\bdocker\s+compose\s+down\b`, `\bsudo\b`, `\bcurl\b.*\|\s*(ba)?sh\b`.
  - `policy.INVESTIGATE_ALLOWED_PREFIXES: list[str]` = `["git log", "git show", "git diff", "git blame", "git status", "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "python -c \"import", "python -m pytest --collect-only"]`.
  - `policy.Policy` dataclass `(worktree: Path, role: Role, allowed_paths: list[str], protected: list[str], bound: dict[str, list[Record]])` where `bound` maps a record glob to its records; methods `relative(file_path: str) -> str | None` (resolve against `worktree`, return the relative posix path or `None` if outside), `path_writable(rel: str) -> tuple[bool, str]` (protected → no; INVESTIGATE → no; TEST → only under `.ateam-scratch/`; BUILD → must match `allowed_paths` or `.ateam-scratch/**`), `command_allowed(cmd: str) -> tuple[bool, str]` (destructive regex over the whole text → no; mentions of `.ateam/` or `.claude/` → no, except `cat`/`head`/`git diff` reads of `.ateam/` which stay allowed; INVESTIGATE → every `;`/`&&`/newline-separated segment must start with an allowed prefix), `async can_use_tool(tool_name, input_data, context)` (Edit/Write/MultiEdit/NotebookEdit → `path_writable`; Bash → `command_allowed`; everything else allow; denials return `PermissionResultDeny(message=f"ATeam: {reason}")` and are appended to `self.denials: list[str]`), `async pre_tool_use(input_data, tool_use_id, context) -> dict` (same deny logic returning `permissionDecision: deny` with reason; for Read/Edit/Write of a path matching a `bound` glob, returns `permissionDecision: allow` plus `additionalContext` with the not-yet-injected records rendered as `ATeam binding record <id>: <statement>`, tracking injected ids in `self.injected: set[str]`), `hook_matchers() -> dict[str, list[HookMatcher]]` (`PreToolUse` matcher `"Edit|Write|MultiEdit|NotebookEdit|Bash|Read"`).
  - `options.clean_env() -> dict[str, str]` (drop every key starting with `CLAUDE`, set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`); `options.worker_options(*, cwd: Path, spec: RoleSpec, brief_text: str, policy: Policy, tier: TierSpec, resume: str | None = None) -> ClaudeAgentOptions` setting exactly the clean-room set from Global Constraints plus `cwd`, `tools=spec.tools`, `system_prompt={"type":"preset","preset":"claude_code","append": spec.system_prompt + "\n\n" + brief_text}`, `output_format={"type":"json_schema","schema": spec.report_schema}`, `can_use_tool=policy.can_use_tool`, `hooks=policy.hook_matchers()`, `model=tier.model`, `effort=tier.effort`, `max_turns=tier.max_turns`, `max_budget_usd=tier.usd`, `resume=resume`.

- [ ] **Step 1: Write the failing tests** (hooks are plain async functions; use `pytest.mark.asyncio`)

```python
# tests/test_policy.py
def test_relative_rejects_escape(policy_build):
    assert policy_build.relative("../outside.txt") is None and policy_build.relative("/etc/passwd") is None
    assert policy_build.relative("src/api/x.py") == "src/api/x.py" and policy_build.relative(str(policy_build.worktree / "src/api/x.py")) == "src/api/x.py"
def test_path_rules_per_role(policy_build, policy_test, policy_investigate):
    assert policy_build.path_writable("src/api/x.py")[0] and not policy_build.path_writable("src/data/y.py")[0]
    assert not policy_build.path_writable(".ateam/contracts/C-0001.yaml")[0]
    assert policy_test.path_writable(".ateam-scratch/notes.md")[0] and not policy_test.path_writable("src/api/x.py")[0]
    assert not policy_investigate.path_writable(".ateam-scratch/notes.md")[0]
def test_command_rules(policy_build, policy_investigate):
    assert not policy_build.command_allowed("echo ok; git push origin main")[0]
    assert not policy_build.command_allowed("echo ok\nrm -rf /tmp/x")[0]
    assert not policy_build.command_allowed("echo hacked > .ateam/contracts/C-0001.yaml")[0]
    assert policy_build.command_allowed("cat .ateam/contracts/C-0001.yaml")[0]
    assert policy_investigate.command_allowed("git log --oneline && ls src")[0] and not policy_investigate.command_allowed("python setup.py")[0]
@pytest.mark.asyncio
async def test_can_use_tool_denies_and_records(policy_build):
    r = await policy_build.can_use_tool("Bash", {"command": "git push"}, None)
    assert isinstance(r, PermissionResultDeny) and policy_build.denials
@pytest.mark.asyncio
async def test_pre_tool_use_injects_once(policy_build):   # policy_build.bound = {"src/api/**": [R-0001]}
    out = await policy_build.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": str(policy_build.worktree / "src/api/x.py")}}, None, None)
    assert "ATeam binding record R-0001" in out["hookSpecificOutput"]["additionalContext"]
    out2 = await policy_build.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": str(policy_build.worktree / "src/api/x.py")}}, None, None)
    assert "additionalContext" not in out2.get("hookSpecificOutput", {})

# tests/test_options.py
def test_clean_env_strips_claude(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1"); monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    e = clean_env(); assert "CLAUDECODE" not in e and e["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
def test_worker_options_clean_room(policy_build, tmp_path):
    o = worker_options(cwd=tmp_path, spec=ROLE_SPECS[Role.build], brief_text="BRIEF", policy=policy_build, tier=TierSpec("haiku", "low", 10, 0.5))
    assert o.setting_sources == [] and o.strict_mcp_config and o.extra_args == {"disable-slash-commands": None} and o.permission_mode == "default"
    assert o.system_prompt["append"].endswith("BRIEF") and o.output_format["schema"] == ROLE_SPECS[Role.build].report_schema and o.tools == ROLE_SPECS[Role.build].tools
```

Fixtures `policy_build`, `policy_test`, `policy_investigate` in `conftest.py` use `target_repo` as worktree, `allowed_paths=["src/api/**"]`, `protected=[".ateam/**", ".claude/**"]`, `bound={"src/api/**": [records_three["R-0001"]]}`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_policy.py tests/test_options.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `runner/policy.py` and `runner/options.py` per Interfaces**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_policy.py tests/test_options.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ateam/runner tests/test_policy.py tests/test_options.py tests/conftest.py
git commit -m "feat: worker policy gate and clean-room session options"
```

---

### Task 11: Tree check, worker runner and `ateam worker run`

**Files:**
- Create: `src/ateam/treecheck.py`, `src/ateam/runner/run.py`, `tests/test_treecheck.py`, `tests/test_run.py`, `tests/live/test_worker_live.py`
- Modify: `src/ateam/cli.py`

**Interfaces:**
- Consumes: `GitRepo`, `Policy`, `worker_options`, `assemble_brief`, `render_brief`, `collect`, `select_records`, `StateDB`, `tiers.resolve`, SDK `query`, `ResultMessage`, `AssistantMessage`, `ToolUseBlock`, `ResultError`.
- Produces:
  - `treecheck.TreeState` dataclass `(base: str, head: str, dirty: list[tuple[str, str]], changed_vs_base: list[str])`; `treecheck.tree_state(git: GitRepo, base: str) -> TreeState`; `treecheck.ScopeResult` dataclass `(ok: bool, outside: list[str], dirty: list[str])`; `treecheck.scope_check(state: TreeState, allowed: list[str], incidental: list[str], ignore: tuple[str, ...] = (".claude/**", ".ateam-scratch/**")) -> ScopeResult` (a path is outside when it matches neither `allowed` nor `incidental`; `dirty` lists every porcelain path not ignored; `ok` is true only when `outside` and `dirty` are both empty).
  - `run.WorkerResult` dataclass `(subtype: str, is_error: bool, terminal_reason: str | None, session_id: str | None, num_turns: int, cost_usd: float, usage: dict, model_usage: dict, report: dict | None, tool_uses: list[dict], denials: list[str], error: str | None, wall_s: float)`; `async run.run_worker(prompt: str, options: ClaudeAgentOptions, policy: Policy) -> WorkerResult` (iterates `query`, collects `ToolUseBlock`s as `{"tool", "input"}`, captures the `ResultMessage`, catches `ResultError` and `CLIConnectionError`-class exceptions into `error` while keeping any result already seen; `report = result.structured_output`).
  - `run.run_contract_attempt(*, paths: TargetPaths, config: TargetConfig, git_worktree: GitRepo, worktree: Path, base: str, contract: Contract, records: dict[str, Record], role: Role, tiers, db: StateDB, resume: str | None = None) -> tuple[WorkerResult, TreeState, ScopeResult, Checkpoint]` (builds policy with `bound` from the contract's frozen record ids, assembles the brief with `latest_checkpoint`, `start_attempt`, runs, `tree_state`, `scope_check`, `collect`, `set_checkpoint`, `finish_attempt` with outcome `"completed"` when `report` is present and `scope.ok`, `"scope_breach"` when the report is present but `scope.ok` is false, else `"failed"`; failure class `None` in pass one; tokens from `usage["input_tokens"]` + `usage.get("cache_read_input_tokens", 0)` and `usage["output_tokens"]`).
  - CLI: `ateam worker run C-0001 --role build|investigate|test --cwd WORKTREE [--base REF] [--tier NAME] [--resume SESSION_ID]` prints the subtype, turns, cost, scope result and the report as JSON; exits 0 on `"completed"`, 3 on scope breach, 1 otherwise. `ateam brief C-0001 --role R` prints `render_brief(...)` without running anything.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_treecheck.py
def test_tree_state_and_scope(target_repo):
    g = GitRepo(target_repo); base = g.head()
    (target_repo / "src/api/x.py").write_text("changed\n"); (target_repo / "notes").mkdir(); (target_repo / "notes/out.txt").write_text("x\n")
    (target_repo / ".ateam-scratch").mkdir(); (target_repo / ".ateam-scratch/handoff.yaml").write_text("progress: x\n")
    st = tree_state(g, base)
    r = scope_check(st, allowed=["src/api/**"], incidental=[])
    assert r.outside == ["notes/out.txt"] and "src/api/x.py" in r.dirty and ".ateam-scratch/handoff.yaml" not in r.dirty and not r.ok
    g.run("add", "src/api/x.py"); g.run("commit", "-q", "-m", "c")
    (target_repo / "notes/out.txt").unlink(); (target_repo / "notes").rmdir()
    r2 = scope_check(tree_state(g, base), allowed=["src/api/**"], incidental=[])
    assert r2.ok and tree_state(g, base).changed_vs_base == ["src/api/x.py"]

# tests/test_run.py  (fake query via monkeypatch; no API)
@pytest.mark.asyncio
async def test_run_worker_collects_result_and_handles_result_error(monkeypatch, policy_build):
    async def fake_query(prompt, options):
        yield AssistantMessage(content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "src/api/x.py"})], model="haiku")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s1", total_cost_usd=0.01, usage={"input_tokens": 5, "output_tokens": 3}, structured_output={"status": "done"})
        raise ResultError("boom", exit_code=1)
    monkeypatch.setattr("ateam.runner.run.query", fake_query)
    r = await run_worker("p", ClaudeAgentOptions(), policy_build)
    assert r.report == {"status": "done"} and r.session_id == "s1" and r.tool_uses[0]["tool"] == "Read" and r.error and "boom" in r.error

# tests/live/test_worker_live.py
pytestmark = pytest.mark.skipif(not os.environ.get("ATEAM_LIVE_TESTS"), reason="set ATEAM_LIVE_TESTS=1")
@pytest.mark.asyncio
async def test_build_worker_end_to_end(live_target):   # fixture: target_repo + approved R-0001 + frozen C-0001 with paths ["src/api/**"], a worktree, tier light
    res, tree, scope, cp = await run_contract_attempt(..., role=Role.build, ...)   # prompt: contract asks to add a comment line to src/api/x.py and commit
    assert res.report and res.report["status"] in {"done", "blocked"} and res.subtype == "success"
    assert scope.ok and tree.changed_vs_base == ["src/api/x.py"]
    assert db.telemetry_rows()[-1]["outcome"] == "completed" and cp.notes is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_treecheck.py tests/test_run.py -v`
Expected: FAIL with import errors

- [ ] **Step 3: Implement `treecheck.py`, `runner/run.py`, and the `worker run` and `brief` CLI commands per Interfaces**

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `.venv/bin/pytest tests -v --ignore=tests/live`
Expected: PASS

- [ ] **Step 5: Run the live test once**

Run: `ATEAM_LIVE_TESTS=1 .venv/bin/pytest tests/live -v -s`
Expected: PASS, printed cost under 0.05 dollars. If it fails on policy denials, read `res.denials` before changing anything.

- [ ] **Step 6: Commit**

```bash
git add src/ateam/treecheck.py src/ateam/runner/run.py src/ateam/cli.py tests/test_treecheck.py tests/test_run.py tests/live
git commit -m "feat: tree check, worker runner and ateam worker run"
```

---

### Task 12: README and run instructions

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`** covering: what ATeam is in three sentences, setup (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`), the clean-room note (claude.ai login works; `--bare` is not used), the command list from Task 8 and Task 11 with one example each, how to run tests (`.venv/bin/pytest`, and the live opt-in), the target repo layout, and a "Status" section stating that discovery, planning, scheduling, verification and integration are pass two and linking the design and this plan.

- [ ] **Step 2: Verify every command in the README runs** (`ateam --help`, `ateam doctor`, `.venv/bin/pytest -q`)

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README with setup, commands and status"
```
