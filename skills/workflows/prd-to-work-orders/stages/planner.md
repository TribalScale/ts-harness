# Stage: Planner

Transform Foundry Blueprints into Work Orders that coding agents can implement independently.

## Inputs

- Complete `docs/refinery/**`.
- Complete `docs/foundry/**`.
- Manifest with `foundry.status = complete`.

## Outputs

```text
docs/planner/
├── epics/
│   ├── CE-001-capability/
│   └── FE-001-feature/
├── context-graph.md
├── execution-phases.md
└── planner-issues.md
```

## Phases

1. **Component epic extraction** — one Component Epic per Component Blueprint.
2. **Feature epic extraction** — one Feature Epic per Feature Blueprint.
3. **Work Order slicing** — produce independently implementable Story-level Work Orders.
4. **Context graph assembly** — connect dependencies, dependents, shared contracts, Blueprints, and FRDs.
5. **Phasing and sequencing** — topologically sort Work Orders into execution phases.
6. **Execution phase approval** — pause for human approval or deferral.
7. **Manifest update** — write or update the `planner` section.

## Work Order Rules

- Requirements are copied verbatim from FRDs.
- Each Work Order has In Scope and Out of Scope sections.
- Each Work Order has E2E acceptance test specs.
- Each Work Order has complete context graph connections.
- Each Work Order is independently implementable unless dependencies are explicit.
- A fresh coding agent should be able to execute the Work Order without clarifying questions.
- Planner never silently edits Blueprints or FRDs. Use `planner-issues.md` for upstream issues.

## Executability Scoring

Each Work Order receives:

| Criterion | Scoring |
|---|---|
| Independence | High if no hidden dependencies beyond context graph. |
| Local testability | High if behavior can be verified locally. |
| Context sufficiency | High if all required specs and contracts are referenced. |
| Scope clarity | High if in/out boundaries are unambiguous. |
| Agent risk | High risk if likely to require human clarification. |

Work Orders with low executability must be split or revised before handoff.

## Quality Gates

Run `quality-gates/planner.md`. Planner is complete only when:

- Every Blueprint has an Epic.
- Every requirement appears verbatim in exactly one Work Order.
- No Work Order is orphaned.
- Dependency graph has no cycles.
- Execution phases respect dependencies.
- Manifest `stages.planner.status` is `complete`.
