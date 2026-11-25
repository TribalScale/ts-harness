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
Complexity: Medium

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
- Follow-up: WO-009 (only when this WO defers part of its job)
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

- **Slice for parallel delivery, at the right size.** Work orders whose dependencies are
  done run at the same time, so delivery takes as long as the longest dependency chain (the
  number of waves), not the number of work orders. Plan the fewest waves you can without
  making any work order too big:
  - Put the shared foundation (project setup, the schema, shared types or API contracts)
    first, in as few work orders as it needs, so the features after it fan out in parallel.
  - Split features along independent seams: separate modules, endpoints, pages or services
    that touch different files. Those run side by side.
  - Keep tightly coupled work together. A change and its only consumer, or two pieces that
    edit the same files, belong in one work order, not a chain of small dependent ones.
  - Declare a dependency only when a work order needs another's code to build or test. "Comes
    later in the PRD" is not a dependency.
  - Do not merge independent features to save a wave. One work order is one coherent feature
    a coding agent can build, test and have reviewed in one session: about 15 files or
    fewer and about 12 requirements or fewer. A bigger one is split along a seam that lets
    both parts run in parallel.
  - The summary states the plan, for example "9 work orders in 3 waves".

- Every WO has a `Complexity:` line right under its heading (under `Repository:` when there
  is one): `Low`, `Medium` or `High`. `High` is for work that is hard to get right, not just
  long: concurrency or transactions, data migrations, security-sensitive logic, intricate
  calculations or state machines, or many interacting modules. TS Harness runs `High` work
  orders on a stronger coding model, so do not mark routine work `High`.
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

- A repository in `input.project_repositories` with `conventions` has rules every work order
  for it must follow (its `AGENTS.md`): folders and layers, how tests are named and written,
  roles, migrations. Name the real paths and patterns they describe in each work order's
  Requirements and Files, rather than inventing a structure.

- A repository with a `template` starts from that TS Harness template: `template_status:
  pending` means it is empty today and TS Harness opens a bootstrap PR, merged before any
  work order runs; `applied` means it already has it. Either way plan on top of it: extend
  its reference module, and plan no work orders for scaffolding, CI, linting or test
  runners, which the template has. Its acceptance tests are Gherkin features named after the
  ticket key. Its app roles start as `Admin` and `User`; when the PRD defines other roles,
  plan a work order that changes them as the conventions' Roles section describes, before
  the work orders that rely on them.

- A repository in `input.project_repositories` with `ci_on_pull_requests: false` has code
  but no CI on pull requests. Plan one work order for it first, "Add CI to <repo>": a GitHub
  Actions workflow on `pull_request` into its base branch that runs what the repository has
  (install, lint, type check, unit tests, its BDD or integration suite, build), and the line
  `require_ci_checks: true` in its `.ts-harness.yml` (create the file if needed), so TS
  Harness makes the jobs required checks once it merges. Every other work order for that
  repository depends on it.

- Testing: each repository in `input.project_repositories` carries `bdd` (true or false),
  decided for this program. TS Harness adds a `## Tests` section to each work order from it
  (`BDD: required` or `BDD: off`); write acceptance tests to match: Given/When/Then criteria
  either way, and unit tests for every rule with its error cases. When `bdd` is true and
  `has_gherkin_runner` is false, the repository's first work order also adds a Gherkin runner.

- When `input.design_refs` is present, every WO that builds or changes UI lists
  the design references it implements in its Context Graph as
  `- Design: D1, D3` (ids from `input.design_refs`, nothing else). The coding
  agent and QA receive exactly those images, so name every frame the WO's UI
  must match and no others. Backend-only WOs omit the line. Describe in the
  Requirements what each image shows that matters (layout, copy, states);
  never write "see the design" alone. A page can bring a hundred frames and only
  the first few images are attached here: choose frames by their titles and
  text in `input.design_refs_text`, including every state the WO builds (empty,
  loading, error, open/closed variants). Write ids only; the harness adds each
  frame's title and Figma link to the work order (`## Design frames`).

- **Deferred work gets its own work order.** A WO may not leave part of its job for "later",
  "for now", "until X exists" or "once Y is ready" in a sentence: nothing tracks that
  sentence, so the stand-in ships. Plan the deferred part as its own follow-up WO that
  depends on the WO producing X, and name it in the deferring WO's Context Graph as
  `- Follow-up: WO-009`. A follow-up may say "once WO-003 is merged" for a WO it depends on.
  The planner contract rejects a deferral without a follow-up, a follow-up that is not in
  the plan, and a follow-up that depends on nothing.

- **API contract first.** An API WO that adds or changes endpoints names them
  (`POST /notes`, `GET /notes/{id}`) and updates the repository's committed `openapi.json`
  as part of its acceptance. A web WO that calls those endpoints depends on that API WO and
  generates its types from the spec: its Requirements say `npm run api:pull && npm run
  api:generate` and use the generated types and typed client. Never plan hand-written API
  types, and never plan "types from the planning docs until the backend's openapi.json is
  copied in": order the WOs so the spec exists first. The planner contract checks each of
  these when the repositories carry roles.

- Before returning, reread each WO as the coding agent, the code reviewer and QA will:
  every one of them checks the code against every statement in the WO. If two
  statements cannot both hold, the reviewer asks for one and QA for the other,
  and the work order loops between them until a person steps in. Gate 12 lists
  how to settle it.

Apply `quality-gates/planner-runtime.md` before returning JSON.
