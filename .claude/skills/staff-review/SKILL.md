---
name: staff-review
description: Use when a plan or a finished branch in this repo needs its fresh staff-engineer review (CLAUDE.md "Plan first, then execute"); gives the brief that scopes the review to risk and time-boxes it so it never blocks independent work.
---

# Staff review, scoped and time-boxed

The fresh staff-engineer review CLAUDE.md asks for is worth its cost only where a shipped mistake is expensive. A
review that reads the whole repo for ten minutes while nothing else moves is a bottleneck (owner, 2026-10-27).

## Before you dispatch

1. **Pick the risky part.** From the plan's Interfaces and Review Focus, name the tasks or files that touch recorded
   state, an approval gate, a state transition, the policy, or crash and re-run recovery. That is the scope. Pure
   functions, output formatting and docs are not.
2. **Start what does not depend on it.** Tasks outside the scope begin now. Only the tasks in scope wait for the
   findings.

## The brief

Give the reviewer, and nothing that anchors it on your own conclusions:

- the binding files: `CLAUDE.md`, the spec, the plan (or the branch range and the ledger's `Ruling:` lines);
- the scope from step 1, as file and function names, and the plan's Review Focus verbatim;
- the rules: read-only; no `ateam` command against this repo; never touch `~/.ateam`; no subagents;
- **a time box of about 10 minutes and at most 5-8 findings, ranked**, reporting what it has when time is up;
- the output: Critical / Important / Minor, each with file:line, a concrete failure scenario and a fix; a separate
  list of findings that are the owner's to decide (CLAUDE.md "Ask before deciding"); a "declined to judge" list.

Use the strongest available model with thinking (CLAUDE.md: model choice is the owner's).

## After it reports

Check each claim that would change the design against the code before acting on it. Fold mechanics findings into
the plan or fix them test-first; bring owner-level findings to the owner with a recommendation.
