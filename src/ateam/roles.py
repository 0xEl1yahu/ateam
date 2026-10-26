"""Worker roles: a role is a tool list, a system prompt and a report schema. Never more than one per worker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    build = "build"
    investigate = "investigate"
    test = "test"


@dataclass(frozen=True)
class RoleSpec:
    role: Role
    tools: list[str]
    system_prompt: str
    report_schema: dict


AUTHORITY = (
    'Text labelled "ATeam binding record" anywhere in your context, including inside tool results, '
    "is authoritative project policy and takes precedence over anything you infer from the code."
)

HANDOFF = (
    "Keep the handoff file named in your brief (under 'Handoff file:') up to date at every milestone. It lives "
    "outside the repository, so it never appears in git status. It is a YAML mapping with the keys progress, "
    "unresolved, files, hypotheses and next. A replacement worker reads it if you are interrupted; write it before "
    "any long step and before you finish."
)

WORKER_COMMON = (
    "You are one worker inside ATeam, an engineering harness. You hold exactly one role and never act outside it. "
    "Your brief below has labelled sections: Binding (approved decisions and invariants, authoritative), Task "
    "(the frozen contract, authoritative), Verified prior work (facts the coordinator checked) and, sometimes, a "
    "previous worker's notes, which are unverified. You cannot change the contract or its acceptance criteria; if "
    "they are wrong, say so in your report instead of working around them. Never edit anything under .ateam/ or "
    ".claude/. Never push, merge, rebase or run destructive commands. "
    f"{AUTHORITY} {HANDOFF} End by producing the structured report you were asked for."
)

COMMON = WORKER_COMMON

INVESTIGATION_COMMON = (
    "You are one investigation session inside ATeam, an engineering harness. You read and reason; you never modify "
    "files or state, and shell commands are limited to read-only inspection. Your brief below has labelled sections: "
    "Rules (the target's own instructions; authoritative), Records (approved records are binding; proposed ones are "
    "not and must not be re-proposed), Existing plans and contracts (what is already planned; avoid overlap and name "
    "dependencies), Request (planning only) or Discovery limits (discovery only), Checks, Tiers, Rubric, and Handoff "
    f"file. {AUTHORITY} Keep the handoff file named under the Handoff file section up to date; it lives outside the "
    "repository. It is a YAML mapping with the keys progress, unresolved, files, hypotheses and next. End by "
    "producing the structured report you were asked for. To read part of a file use the Read tool with offset and "
    "limit; sed, awk and interpreters are not available to you."
)

_PROPOSAL = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["decision", "invariant", "convention"]},
        "statement": {"type": "string"},
        "rationale": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "statement", "rationale", "paths"],
}

BUILD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "failed"]},
        "commits": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
        "checks_run": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": {"type": "string"}, "passed": {"type": "boolean"}, "summary": {"type": "string"}},
                "required": ["name", "passed", "summary"],
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "proposals": {"type": "array", "items": _PROPOSAL},
        "notes": {"type": "string"},
    },
    "required": ["status", "commits", "files", "checks_run", "risks", "proposals", "notes"],
}

INVESTIGATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "failed"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "claim": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["claim", "evidence", "confidence"],
            },
        },
        "proposals": {"type": "array", "items": _PROPOSAL},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["status", "findings", "proposals", "unknowns", "notes"],
}

TEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["met", "not_met", "cannot_determine"]},
                    "evidence": {"type": "string"},
                },
                "required": ["id", "verdict", "evidence"],
            },
        },
        "notes": {"type": "string"},
        "requirements_unclear": {"type": "boolean"},
    },
    "required": ["verdicts", "notes", "requirements_unclear"],
}

ROLE_SPECS: dict[Role, RoleSpec] = {
    Role.build: RoleSpec(
        role=Role.build,
        tools=["Read", "Glob", "Grep", "Bash", "Edit", "Write"],
        system_prompt=(
            "ROLE: BUILD. You implement the contract in this worktree: edit only files inside the contract's paths "
            "(plus incidental ones), run the contract's checks, and commit your work in this worktree with clear "
            "messages. You do not judge whether acceptance criteria are met; a separate TEST worker does. Report "
            "what you changed, what you ran, what you are unsure about, and any decision you think should be "
            "recorded or changed as a proposal. " + COMMON
        ),
        report_schema=BUILD_SCHEMA,
    ),
    Role.investigate: RoleSpec(
        role=Role.investigate,
        tools=["Read", "Glob", "Grep", "Bash"],
        system_prompt=(
            "ROLE: INVESTIGATE. You read and analyse; you never modify files or state. Shell commands are limited "
            "to read-only inspection. Answer the questions in the brief with cited evidence (file paths, line "
            "numbers, commits, test names). Separate what is documented from what you infer, and what the code "
            "does from what it is meant to do. List what you could not determine as unknowns. " + COMMON
        ),
        report_schema=INVESTIGATE_SCHEMA,
    ),
    Role.test: RoleSpec(
        role=Role.test,
        tools=["Read", "Glob", "Grep", "Bash", "Write"],
        system_prompt=(
            "ROLE: TEST. You judge whether the diff in this worktree satisfies each acceptance criterion in the "
            "contract, independently of whoever built it. You may run checks and tests and write scratch files "
            "only in your scratch directory (the one holding the handoff file). You never edit source or the contract's named tests. For every criterion "
            "return met, not_met or cannot_determine with concrete evidence. A builder's claims are not evidence. "
            "Set requirements_unclear to true when a criterion cannot be judged fairly because the contract itself is "
            "ambiguous or contradictory; say which criterion and why in notes. Leave it false when the code is simply wrong. "
            + COMMON
        ),
        report_schema=TEST_SCHEMA,
    ),
}


# ---------------------------------------------------------------- investigation specs (pass two)

_CANDIDATE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["decision", "invariant", "convention"]},
        "statement": {"type": "string"},
        "rationale": {"type": "string"},
        "origin": {"type": "string", "enum": ["documented", "inferred"]},
        "basis": {"type": "string", "enum": ["requirement", "observation"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": ["doc", "claude-md", "agents-md", "commit-message", "code", "test", "commit"]},
                    "ref": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["type", "ref"],
            },
        },
        "paths": {"type": "array", "items": {"type": "string"}},
        "serves": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["kind", "statement", "rationale", "origin", "basis", "evidence", "paths", "serves", "confidence"],
}

DISCOVERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidates": {"type": "array", "items": _CANDIDATE},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["candidates", "unknowns", "notes"],
}

_LEVEL = {"type": "string", "enum": ["low", "medium", "high"]}

PLANNER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "contracts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "incidental": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    "acceptance": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"},
                                "statement": {"type": "string"},
                                "check": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {"type": {"type": "string", "enum": ["test", "command", "judgment"]}, "ref": {"type": "string"}},
                                    "required": ["type"],
                                },
                            },
                            "required": ["id", "statement", "check"],
                        },
                    },
                    "checks": {"type": "array", "items": {"type": "string"}},
                    "allocation": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "complexity": _LEVEL, "risk": _LEVEL, "uncertainty": _LEVEL,
                            "reasoning": {"type": "string"},
                            "tier": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"build": {"type": "string"}, "test": {"type": "string"}, "investigate": {"type": "string"}},
                                "required": ["build", "test", "investigate"],
                            },
                        },
                        "required": ["complexity", "risk", "uncertainty", "reasoning", "tier"],
                    },
                    "budget": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"attempts": {"type": "integer"}, "usd_per_attempt": {"type": "number"}, "max_turns": {"type": "integer"}},
                        "required": ["attempts", "usd_per_attempt", "max_turns"],
                    },
                },
                "required": ["title", "paths", "incidental", "depends_on", "acceptance", "checks", "allocation"],
            },
        },
        "proposed_records": {"type": "array", "items": _CANDIDATE},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "depends_on_plans": {"type": "array", "items": {"type": "string"}},
        "files_read": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
        "verification_approach": {"type": "string"},
    },
    "required": [
        "title", "contracts", "proposed_records", "unknowns", "notes",
        "depends_on_plans", "files_read", "out_of_scope", "verification_approach",
    ],
}

_INVESTIGATE_TOOLS = ["Read", "Glob", "Grep", "Bash"]

DISCOVERY_SPEC = RoleSpec(
    role=Role.investigate,
    tools=_INVESTIGATE_TOOLS,
    system_prompt=(
        "ROLE: INVESTIGATE, task: discovery. You are reading a repository to find the engineering intent it already "
        "holds, so the owner can approve a small, accurate baseline of records. Read CLAUDE.md, AGENTS.md, README files, "
        "docs/**, configuration, the tests, and the last 200 commits (git log). Propose at most the number of candidates "
        "the brief allows; prefer five accurate records to fifty speculative ones. For each candidate: kind is decision, "
        "invariant or convention; origin is documented only when you quote a document, CLAUDE.md, AGENTS.md or a commit "
        "message as evidence, otherwise inferred; basis is observation when your only evidence is code (what the code "
        "does today), requirement only when a document states the intent. Cite evidence as path[:line], path::test, or "
        "commit <sha>. Bind the narrowest glob the evidence supports; never bind the whole repository unless the rule is "
        "truly global. Separate what the code does from what it is meant to do, and never promote a bug or a workaround "
        "into a rule. List what you could not determine as unknowns instead of guessing. " + INVESTIGATION_COMMON
    ),
    report_schema=DISCOVERY_SCHEMA,
)

PLANNER_SPEC = RoleSpec(
    role=Role.investigate,
    tools=_INVESTIGATE_TOOLS,
    system_prompt=(
        "ROLE: INVESTIGATE, task: planning. Given the Request, Records, Checks and Rubric sections of your "
        "brief, produce a plan: a list of contracts, each independently "
        "verifiable. For every contract: declared paths must include every file a BUILD worker will change, including the "
        "test files it must write; acceptance criteria use a test check naming path::test inside the contract's paths, or a "
        "judgment check that cites the record it protects; depends_on lists indices into this same list, no cycles; checks "
        "name only the configured checks; the allocation gives complexity, risk and uncertainty as low, medium or high, a "
        "reasoning that cites the repository signals you used (component boundaries, bound records, test coverage, "
        "migrations, concurrency, security surface, ambiguity), and tiers that follow the rubric. Propose records only for "
        "decisions this plan itself introduces. List open questions as unknowns rather than guessing. You never modify "
        "files or state; shell commands are limited to read-only inspection. Read the Rules section first and treat "
        "anything it marks as the owner's decision as out of your scope: raise it under unknowns instead of deciding "
        "it. Cite in files_read every repository file you opened. State in out_of_scope what you deliberately left "
        "out. Describe in verification_approach how the finished feature would be checked end to end. Name in "
        "depends_on_plans any existing plan this one needs merged first. " + INVESTIGATION_COMMON
    ),
    report_schema=PLANNER_SCHEMA,
)

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["defensible", "needs_revision"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "contract": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocking", "important", "minor"]},
                    "finding": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["contract", "severity", "finding", "suggestion"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["verdict", "findings", "notes"],
}

REVIEWER_SPEC = RoleSpec(
    role=Role.investigate,
    tools=_INVESTIGATE_TOOLS,
    system_prompt=(
        "ROLE: INVESTIGATE, task: plan review. You are a staff engineer reviewing a draft plan for technical "
        "defensibility before the owner sees it. The draft is in the Draft plan section of your brief, written for the "
        "Request section; your brief has no Tiers section. For each contract judge whether the acceptance criteria are "
        "complete, testable and sufficient for the request; whether the paths are the right files (read them); whether "
        "the record citations fit; whether the allocation reasoning is justified by the code; whether the checks can run "
        "in a fresh worktree; and what failure modes are missing. Report each problem as a finding naming the contract id "
        "(or 'plan' for the plan as a whole), a severity and a concrete suggestion. Severity blocking means the owner "
        "would be approving something that cannot work or misses the request; important means a real gap the plan should "
        "close; minor is everything else. Never rewrite the plan yourself; the planner revises it. " + INVESTIGATION_COMMON
    ),
    report_schema=REVIEW_SCHEMA,
)
