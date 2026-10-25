# autoresearchOSpy integration research

Date: 2026-10-25  
Status: research and recommendations; not an approved implementation plan  
ATeam baseline: `84fd25f`  
Upstream revision reviewed: `84515f988a0afe072ca6aeda488716f48a15461f`

## Recommendation

Integrate the experiment-loop pattern as an ATeam-owned evaluation facility. Reuse candidate proposal and baseline-comparison ideas; replace upstream execution, persistence, constraints, and Git operations with mechanisms compatible with ATeam.

The first experiment should optimize the queue intake classifier prompt described in [the queue integration research](2026-10-25-queue-switchclaude-integration-research.md). This offers a narrow editable asset and labelled examples before ATeam has an automated verifier for engineering work.

## What upstream implements

[autoresearchOSpy](https://github.com/0xEl1yahu/autoresearchOSpy) contains a standalone Python script that reads `agent.yaml`, `evals.yaml`, and `program.md`; asks an optimizer model for a complete replacement agent configuration; evaluates model responses; and retains improvements in Git. Providers include OpenRouter, Anthropic, and OpenAI through direct HTTP calls. Dependencies are PyYAML and httpx.

Its metric is assertion pass rate across text-response tests. It does not execute ATeam contracts, tool-using workers, deterministic repository checks, or independent TEST workers.

Source: [reviewed implementation](https://github.com/0xEl1yahu/autoresearchOSpy/blob/84515f988a0afe072ca6aeda488716f48a15461f/autoresearch_agents.py).

## Proposed architecture

```text
Fixed evaluation suite + baseline + approved experiment boundaries
                          |
                    Candidate proposal
                          |
                 Schema and invariant validation
                          |
                   Isolated evaluation
                          |
             Quality, cost, and latency comparison
                          |
               Evidence + reviewable candidate diff
                          |
                    Owner-reviewed promotion
```

ATeam remains responsible for worker permissions, frozen contracts, verification, and integration. Experiments must not modify approved records, policy code, approval gates, or acceptance criteria merely to improve a score.

Use separate experiment identity, candidate hash, dataset version, model/settings, budgets, and evaluation results. Proposed durable experiment definitions belong in the private intent repository; operational results belong under the ATeam runtime directory. Exact storage and approval semantics require owner agreement before implementation.

## Existing integration points

| ATeam file | Role in integration |
|---|---|
| `src/ateam/roles.py` | Role prompts and structured report schemas; extract only explicitly editable prompt sections |
| `src/ateam/planner.py` | Validate candidate planner outputs against dependency, path, check, and allocation rules |
| `src/ateam/discovery.py` | Validate discovery outputs and proposed records |
| `src/ateam/allocation.py` | Preserve deterministic allocation constraints |
| `src/ateam/tiers.py` and `src/ateam/config.py` | Existing model, effort, turn, and cost settings; changes remain owner-controlled |
| `src/ateam/runner/run.py` | Existing SDK execution envelope for eventual worker-level evaluations |
| `src/ateam/state/db.py` | Existing attempt telemetry; add experiment provenance if adopted |

Current attempt outcome `completed` means a report exists and scope checks pass. It does not mean acceptance criteria passed; checkpoint collection currently receives `checks=[]`. Do not train or select candidates using that field as a quality-success label.

## Required changes to upstream behaviour

1. **Scoped Git operations.** Upstream stages all changes with `git add -A` and discards tracked workspace changes with `git checkout -- .`. Its Git wrapper does not check command success. Replace these with explicit candidate paths, isolated worktrees, and checked operations.
2. **Real constraints.** Preserving `# CONSTRAINT:` comments does not enforce their meaning. Validate allowed fields, values, immutable sections, and policy invariants programmatically.
3. **Fail-closed evaluation.** Unknown assertion types currently pass. Reject malformed or unsupported assertions and empty evaluation suites.
4. **Effective parameters.** `temperature` is read but never passed to the model call. Only expose parameters actually consumed by the evaluation adapter.
5. **Reliable scoring.** Add held-out examples and repeated trials. Require quality floors and zero critical invariant violations before comparing cost or latency. A single observed improvement is insufficient evidence for promotion.
6. **Explicit model access.** Direct provider API keys are separate from ATeam's Claude subscription login. Add an explicit adapter and approved model configuration; do not assume authentication is interchangeable.
7. **Bounded spending.** Account for optimizer calls, candidate evaluations, retries, and the whole experiment, not just a worker attempt.
8. **No import-time installation.** Upstream attempts to install missing dependencies during import. Package dependencies normally instead of importing the script wholesale.

## Suggested implementation order

1. Define experiment and result schemas, immutable evaluation fixtures, and baseline reporting.
2. Add a classifier evaluator using queue intake fixtures; separate training examples from held-out cases.
3. Add candidate proposal, allowlisted edits, and bounded evaluation orchestration.
4. Produce a reviewable diff and evidence report without automatic production promotion.
5. Extend to planner and discovery prompts using schema validation and semantic assertions.
6. Extend to BUILD/TEST behaviour only after ATeam's scheduler and verifier exist.

Proposed new modules: `src/ateam/experiments/model.py`, `evaluator.py`, and `optimizer.py`. These are proposed paths, not existing interfaces.

## Validation gates

- Invalid candidate schemas, unknown assertions, changed constraints, and exhausted budgets stop the experiment.
- Evaluation cannot alter its own fixtures or unrelated files.
- Every candidate is traceable to a baseline, dataset version, and effective model settings.
- Held-out performance and repeated trials support the claimed improvement.
- An experiment never marks a contract verified or promotes a production configuration by itself.
- Worker-level evaluation eventually uses deterministic checks plus the independent TEST role.

## Decisions reserved for the owner

Approve the editable assets, allowed models, aggregate budget, storage/worktree workflow, promotion rules, and whether any bounded experiment can run without per-candidate approval. This research does not change those policies.

## Verification limits

Source inspection only. No upstream optimizer was executed, no paid model calls were made, and no runtime compatibility or performance claim has been established. The upstream source and README describe the project as MIT-licensed; preserve attribution and confirm license material before copying code.
