# Allocation rubric

Three dimensions, each Low, Medium or High. The planner scores every contract on all three, cites the
repository signals it used, and the coordinator turns the scores into tiers with the mappings below.
The owner sees scores, reasoning and tiers at plan approval and may override any tier.

File count alone is not a signal. A one-line change to a payment boundary is High risk; a two-hundred-line
rename inside one module is Low.

## Complexity: how hard is the implementation?

| Level | Signals |
|---|---|
| Low | one component, existing patterns to copy, no new dependency, no data model change |
| Medium | two components or one boundary (API, storage, queue), a new dependency, a migration that only adds |
| High | cross-cutting change, concurrency or ordering concerns, a migration that rewrites or deletes data, a new abstraction the codebase lacks |

## Risk: what goes wrong if the change is incorrect?

| Level | Signals |
|---|---|
| Low | reversible, covered by existing tests on the touched paths, no approved record bound to the paths |
| Medium | touched paths bound by an approved record, partial test coverage, user-visible behaviour changes |
| High | data loss or corruption possible, security surface (auth, secrets, input parsing), payments, external side effects, no tests on the touched paths |

## Uncertainty: how well understood are requirements, dependencies and current behaviour?

| Level | Signals |
|---|---|
| Low | request is explicit, acceptance criteria are testable as written, the code path is read and understood |
| Medium | one open question about behaviour or dependencies that reading more code could settle |
| High | the request is ambiguous, the current behaviour is undocumented or surprising, or an external system is involved that nobody has inspected |

## Mappings (applied by the coordinator)

| Dimension | Low | Medium | High |
|---|---|---|---|
| Complexity -> BUILD tier | light | standard | careful |
| Risk -> TEST tier | at least the BUILD tier | at least standard | careful |
| Uncertainty -> INVESTIGATE tier | none | light | standard |

TEST never runs below the BUILD it checks. High uncertainty with questions still open after investigation
pauses the plan for the owner rather than guessing.
