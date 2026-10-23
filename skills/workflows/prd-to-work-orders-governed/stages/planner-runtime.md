# Stage: Planner (Governed Runtime)

Transform Foundry JSON artifacts into implementation-ready Work Orders.

## Inputs

- `input.approved_prd` — source PRD
- `input.prior_refinery` — refinery artifacts
- `input.prior_foundry` — foundry artifacts
- `input.stage` — must be `planner`
- `input.project_repositories` — the project's repositories (`full_name`, `role`, `default_branch`); absent for single-repository projects

## Outputs (JSON artifacts)

| Key | Content |
|---|---|
| `work_orders_markdown` | All work orders (`# WO-001 Title` sections) |
| `context_graph_markdown` | Dependencies, dependents, shared contracts |
| `execution_phases_markdown` | Topologically sorted phases |

## Top-level output fields

Also set on the task output (not only inside `artifacts`):

- `work_order_ids`: `["WO-001", "WO-002", ...]`
- `planner_complete`: `true`

## Phases

1. **Epic extraction** — component epics (`CE-NNN`) and feature epics (`FE-NNN`).
2. **Work Order slicing** — independently implementable story-level WOs.
3. **Context graph** — link WOs to blueprints, FRDs, dependencies.
4. **Execution phasing** — topological sort into phases.

## Work Order Template (inline)

Each WO in `work_orders_markdown`:

```markdown
# WO-001 Title

Repository: owner/name

## Summary
...

## Requirements
(verbatim from FRD)

## Technical Constraints
(verbatim from the Technical Constraints in input.prior_refinery requirements and the PRD)

## In Scope
...

## Out of Scope
...

## E2E Acceptance Tests
Given ... When ... Then ...

## Context Graph
- Depends on: WO-002
- Blueprints: FB-001
- FRDs: FRD-001
```

## Rules

- Requirements copied verbatim from FRDs in `input.prior_refinery`.
- Every WO repeats the full Technical Constraints list. A coding agent sees only
  its own WO, so a constraint missing there (for example the framework) is lost.
- Work Orders build on one shared codebase: the first WO sets up the project in
  the stated stack and later WOs depend on it instead of creating parallel
  projects.
- Each WO independently implementable unless dependencies are explicit.
- A fresh coding agent should execute without clarifying questions.
- Do not silently edit blueprints or FRDs; note issues in `warnings`.
- When `input.project_repositories` is present, every WO starts with a
  `Repository: owner/name` line naming exactly one of those repositories (its
  `full_name`). Use `role` to place the work: UI work in the web repository,
  endpoints in the API repository. A WO that spans two repositories is two WOs,
  the consumer depending on the provider. With a single repository the line may
  be omitted.
- Dependencies between repositories go one way. If WOs in `acme/web` depend on
  `acme/api`, no `acme/api` WO may depend on `acme/web`; move the shared piece
  so the dependency points one way.

Apply `quality-gates/planner-runtime.md` before returning JSON.
