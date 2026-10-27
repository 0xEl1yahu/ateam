"""Run one worker session and one contract attempt end to end."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock, query

from ateam.brief import assemble_brief, render_brief
from ateam.checkpoint import Checkpoint, collect
from ateam.config import TargetConfig
from ateam.contracts.model import Contract
from ateam.gitops import GitRepo
from ateam.guidance import live_guidance, load_guidance
from ateam.intent import intent_git
from ateam.paths import TargetPaths
from ateam.records.model import Record
from ateam.roles import ROLE_SPECS, Role
from ateam.runner.binding import require_binding
from ateam.runner.options import worker_options
from ateam.runner.policy import Policy
from ateam.state.db import StateDB
from ateam.tiers import TierSpec, resolve
from ateam.treecheck import ScopeResult, TreeState, scope_check, tree_state


@dataclass
class WorkerResult:
    subtype: str
    is_error: bool
    terminal_reason: str | None
    session_id: str | None
    num_turns: int
    cost_usd: float
    usage: dict[str, Any]
    model_usage: dict[str, Any]
    report: dict | None
    tool_uses: list[dict]
    denials: list[str]
    error: str | None
    wall_s: float = 0.0
    text: str = field(default="")


async def run_worker(prompt: str, options: ClaudeAgentOptions, policy: Policy) -> WorkerResult:
    t0 = time.time()
    tool_uses: list[dict] = []
    result: ResultMessage | None = None
    error: str | None = None
    try:
        async for m in query(prompt=prompt, options=options):
            if isinstance(m, AssistantMessage):
                for block in m.content:
                    if isinstance(block, ToolUseBlock):
                        tool_uses.append({"tool": block.name, "input": block.input})
            elif isinstance(m, ResultMessage):
                result = m
    except Exception as e:  # noqa: BLE001  (ResultError, CLI errors, transport errors)
        error = f"{type(e).__name__}: {e}"
    wall = time.time() - t0
    if result is None:
        return WorkerResult("no_result", True, None, None, 0, 0.0, {}, {}, None, tool_uses, list(policy.denials), error, wall)
    model_usage = {k: (v.__dict__ if hasattr(v, "__dict__") else v) for k, v in (result.model_usage or {}).items()}
    report = result.structured_output if isinstance(result.structured_output, dict) else None
    return WorkerResult(
        subtype=result.subtype, is_error=bool(result.is_error), terminal_reason=result.terminal_reason,
        session_id=result.session_id, num_turns=result.num_turns, cost_usd=float(result.total_cost_usd or 0.0),
        usage=dict(result.usage or {}), model_usage=model_usage, report=report, tool_uses=tool_uses,
        denials=list(policy.denials), error=error, wall_s=wall, text=result.result or "",
    )


def build_policy(
    worktree: Path, role: Role, contract: Contract, config: TargetConfig, records: dict[str, Record], scratch_dir: Path | None = None
) -> Policy:
    bound: dict[str, list[Record]] = {}
    for ref in contract.records:
        r = records.get(ref.id)
        if r is None:
            continue
        for g in r.binds.paths:
            bound.setdefault(g, []).append(r)
    return Policy(
        worktree=worktree, role=role, allowed_paths=list(contract.paths), protected=list(config.protected_paths),
        bound=bound, scratch_dir=scratch_dir, ateam_home=scratch_dir.parent.parent.parent if scratch_dir else None,
    )


class SetupError(Exception):
    """A config.setup command failed; nothing is recorded, so the next run tries again."""

    def __init__(self, cmd: str, returncode: int, output: str) -> None:
        super().__init__(f"setup failed: {cmd} (exit {returncode})")
        self.cmd = cmd
        self.returncode = returncode
        self.output = output


SETUP_TIMEOUT_S = 600


def setup_key(worktree: Path, base: str) -> str:
    """The state-store key that marks setup done for this worktree on this base: its resolved path plus the identity
    (inode and change time) of its `.git` entry, so a worktree removed and recreated at the same path runs setup again,
    plus the base, so a lane's worktree runs it again for each contract on a new base (owner, 2026-10-27)."""
    resolved = worktree.resolve()
    marker = resolved / ".git"
    st = marker.stat() if marker.exists() else resolved.stat()
    return f"setup:{resolved}:{st.st_ino}:{st.st_ctime_ns}:{base}"


def ensure_setup(worktree: Path, config: TargetConfig, db: StateDB, base: str, on_command=None) -> list[str]:
    """Run config.setup once per worktree and base; return the commands run ([] when already done or none configured).
    Each command has SETUP_TIMEOUT_S seconds; a timeout is a SetupError."""
    key = setup_key(worktree, base)
    if not config.setup or db.get_meta(key):
        return []
    env = {**os.environ, **config.env}
    for cmd in config.setup:
        if on_command:
            on_command(cmd)
        try:
            proc = subprocess.run(cmd, shell=True, cwd=worktree, env=env, capture_output=True, text=True, timeout=SETUP_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            out = out.decode(errors="replace") if isinstance(out, bytes) else out
            raise SetupError(cmd, -1, f"{out}timed out after {SETUP_TIMEOUT_S} s") from None
        if proc.returncode != 0:
            raise SetupError(cmd, proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
    db.set_meta(key, datetime.now(timezone.utc).isoformat())
    return list(config.setup)


def classify_test_report(report: dict) -> tuple[str, str | None]:
    """Outcome and failure class of a TEST attempt. Only not_met fails it; cannot_determine alone stays completed."""
    if not any(v.get("verdict") == "not_met" for v in report.get("verdicts", [])):
        return "completed", None
    return "failed", ("requirements" if report.get("requirements_unclear") else "implementation")


def default_base(git_worktree: GitRepo, target_branch: str) -> str:
    """The commit the worktree forked from: merge-base with the target branch, else HEAD."""
    try:
        return git_worktree.run("merge-base", target_branch, "HEAD").strip()
    except Exception:  # noqa: BLE001
        return git_worktree.head()


def prepare_scratch(paths: TargetPaths, contract_id: str, role: Role = Role.build) -> Path:
    """The worker's scratch directory, under ATeam's home, never inside the worktree or the target. TEST has its
    own (`<id>-test`, at the same depth), so it never reads the BUILD handoff from disk."""
    scratch = paths.scratch_root / (f"{contract_id}-test" if role is Role.test else contract_id)
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def default_prompt(contract: Contract, role: Role) -> str:
    verb = {Role.build: "Implement", Role.investigate: "Investigate", Role.test: "Judge"}[role]
    return f"{verb} contract {contract.id}: {contract.title}. Follow the brief in your system prompt and end with the structured report."


async def run_contract_attempt(
    *, paths: TargetPaths, config: TargetConfig, git_worktree: GitRepo, worktree: Path, base: str,
    contract: Contract, records: dict[str, Record], role: Role, tiers: dict[str, dict[Role, TierSpec]],
    db: StateDB, resume: str | None = None, prompt: str | None = None, tier_override: str | None = None,
) -> tuple[WorkerResult, TreeState, ScopeResult, Checkpoint]:
    require_binding(contract, records, db)
    if tier_override:
        if tier_override not in tiers:
            raise ValueError(f"unknown tier {tier_override!r}")
        tier, tier_name = tiers[tier_override][role], tier_override
    else:
        tier, tier_name = resolve(contract, role, tiers), contract.allocation.tier[role.value]
    scratch = prepare_scratch(paths, contract.id, role)
    policy = build_policy(worktree, role, contract, config, records, scratch_dir=scratch)
    selected = [records[ref.id] for ref in contract.records if ref.id in records]
    # A BUILD retry reads BUILD's own notes; TEST sees facts only (its brief drops notes).
    prev_raw = db.latest_checkpoint(contract.id, role="build") if role is Role.build else db.latest_checkpoint(contract.id)
    prev = Checkpoint.from_dict(prev_raw) if prev_raw else None
    instructions = live_guidance(load_guidance(paths.guidance, contract.id), contract.content_hash()) if role is Role.build else []
    brief_text = render_brief(assemble_brief(contract, selected, prev, role, scratch_dir=scratch, instructions=instructions))
    predicted = {k: contract.allocation.model_dump()[k] for k in ("complexity", "risk", "uncertainty")}
    attempt_id = db.start_attempt(contract.id, role.value, tier_name, tier.model, predicted)

    intent = intent_git(paths)
    intent_before = (intent.head(), intent.is_clean())
    options = worker_options(cwd=worktree, spec=ROLE_SPECS[role], brief_text=brief_text, policy=policy, tier=tier, resume=resume)
    res = await run_worker(prompt or default_prompt(contract, role), options, policy)
    intent_after = (intent.head(), intent.is_clean())
    tampered = intent_after != intent_before or not intent_after[1]

    usage = res.usage
    tokens_in = int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_read_input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    try:
        tree = tree_state(git_worktree, base)
        scope = scope_check(tree, contract.paths, contract.incidental)
        cp = collect(git_worktree, scratch, base, contract, records, res.session_id, checks=[])
        db.set_checkpoint(contract.id, attempt_id, cp.to_dict())
    except Exception:
        # The session was already paid for: close the attempt before propagating.
        db.finish_attempt(attempt_id, "failed", "environment", tokens_in, tokens_out, res.cost_usd, res.num_turns, res.wall_s, res.session_id)
        raise
    if tampered:
        outcome, failure_class = "tamper", "tamper"   # the intent repo changed during the session; nothing a worker may do
    else:
        outcome = "completed" if res.report and scope.ok else ("scope_breach" if res.report else "failed")
        failure_class = None
        if role is Role.test and res.report and scope.ok:
            outcome, failure_class = classify_test_report(res.report)
    db.finish_attempt(attempt_id, outcome, failure_class, tokens_in, tokens_out, res.cost_usd, res.num_turns, res.wall_s, res.session_id)
    return res, tree, scope, cp
