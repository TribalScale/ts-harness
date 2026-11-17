# Stage: Planner (Governed Runtime)

Transform Foundry JSON artifacts into implementation-ready Work Orders.

## Inputs

- `input.approved_prd` — source PRD
- `input.prior_refinery` — refinery artifacts
- `input.prior_foundry` — foundry artifacts
- `input.stage` — must be `planner`
- `input.project_repositories` — the project's repositories (`full_name`, `role`, `default_branch`); absent for single-repository projects
- `input.design_refs` / `input.design_refs_text` — design references (Figma frames, attached images) with ids `D1`, `D2`…; the images follow the task message

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
- Design: D1, D3
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
  `Repository:` line, right under its heading, naming exactly one of those
  repositories by its `full_name` (for example `Repository: acme/web`). Use
  `role` to place the work: UI work in the web repository, endpoints in the API
  repository. A WO that spans two repositories is two WOs, the consumer
  depending on the provider. With a single repository the line may be omitted.
- Without `input.project_repositories`, leave the `Repository:` line out. Never
  write a placeholder such as `owner/name`; the planner contract rejects it.
- Dependencies between repositories go one way. If WOs in `acme/web` depend on
  `acme/api`, no `acme/api` WO may depend on `acme/web`; move the shared piece
  so the dependency points one way.

- When `input.design_refs` is present, every WO that builds or changes UI lists
  the design references it implements in its Context Graph as
  `- Design: D1, D3` (ids from `input.design_refs`, nothing else). The coding
  agent and QA receive exactly those images, so name every frame the WO's UI
  must match and no others. Backend-only WOs omit the line. Describe in the
  Requirements what each image shows that matters (layout, copy, states);
  never write "see the design" alone.

- Before returning, reread each WO as the coding agent, the code reviewer and QA will:
  every one of them checks the code against every statement in the WO. If two
  statements cannot both hold, the reviewer asks for one and QA for the other,
  and the work order loops between them until a person steps in. Gate 12 lists
  how to settle it.

Apply `quality-gates/planner-runtime.md` before returning JSON.
