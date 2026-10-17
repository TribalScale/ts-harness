# Pipeline State Machine

The umbrella skill uses this state machine before every action. Folder presence is not enough; a state is only complete when required artifacts exist, quality gates pass, and the manifest records the completed stage.

## States

| State | Detection | Required response |
|---|---|---|
| `NO_INPUT` | No PRD path, pasted PRD, or source PRD in the manifest. | Ask for the PRD before doing stage work. |
| `PRD_READY` | A PRD exists and `refinery.status` is missing or not `complete`. | Run Refinery. |
| `REFINERY_IN_PROGRESS` | Some `docs/refinery/**` files exist, but required outputs or gates are incomplete. | Offer `resume` or `regenerate`. |
| `REFINERY_COMPLETE` | Overview docs, feature tree, FRDs, assumptions, consistency report, gates, and manifest are complete. | Run Foundry. |
| `FOUNDRY_IN_PROGRESS` | Some `docs/foundry/**` files exist, but Blueprints, challenges, gates, or manifest are incomplete. | Offer `resume` or `regenerate`. |
| `FOUNDRY_COMPLETE` | Containers, Components, Feature Blueprints, architecture summary, challenge resolution, gates, and manifest are complete. | Run Planner. |
| `PLANNER_IN_PROGRESS` | Some `docs/planner/**` files exist, but epics, Work Orders, context graph, execution phases, gates, or manifest are incomplete. | Offer `resume` or `regenerate`. |
| `PLANNER_COMPLETE` | Epics, Work Orders, context graph, execution phases, gates, and manifest are complete. | Hand off to implementation or run Validator if code exists. |
| `IMPLEMENTATION_PRESENT` | Planner is complete and application code exists, but validator report for the target scope is missing or stale. | Run Validator. |
| `VALIDATOR_COMPLETE` | Drift report exists for declared scope and every item has a CWO, resolution, or human escalation. | Continue with CWOs or finish. |

## Required Artifact Checks

### Refinery Complete

- `docs/refinery/overview/business-problem.md`
- `docs/refinery/overview/current-state.md`
- `docs/refinery/overview/personas.md`
- `docs/refinery/overview/product-description.md`
- `docs/refinery/overview/success-metrics.md`
- `docs/refinery/overview/technical-requirements.md`
- `docs/refinery/features/FRD-*.md`
- `docs/refinery/feature-tree.md`
- `docs/refinery/working-assumptions.md`
- `docs/refinery/phase-5-cross-frd-consistency.md`

### Foundry Complete

- `docs/foundry/containers/*.md`
- `docs/foundry/components/*.md`
- `docs/foundry/features/FB-*.md`
- `docs/foundry/architectural-challenges.md`
- `docs/foundry/architecture-summary.md`

### Planner Complete

- `docs/planner/epics/**/epic.md`
- `docs/planner/epics/**/WO-*.md`
- `docs/planner/context-graph.md`
- `docs/planner/execution-phases.md`

### Validator Complete

- `docs/validator/drift-reports/*.md`
- `docs/validator/corrective-work-orders/*.md` when minor drift exists
- Major drift entries have explicit human decisions

## Transition Rules

- `NO_INPUT -> PRD_READY` only after a source PRD is provided.
- `PRD_READY -> REFINERY_IN_PROGRESS -> REFINERY_COMPLETE`.
- `REFINERY_COMPLETE -> FOUNDRY_IN_PROGRESS -> FOUNDRY_COMPLETE`.
- `FOUNDRY_COMPLETE -> PLANNER_IN_PROGRESS -> PLANNER_COMPLETE`.
- `PLANNER_COMPLETE -> IMPLEMENTATION_PRESENT` when implementation code exists.
- `IMPLEMENTATION_PRESENT -> VALIDATOR_COMPLETE`.

If an upstream source hash changes, downstream stages become `STALE` overlays. A stale overlay requires `patch`, `regenerate`, or `migrate` before the stage can be trusted.

## Stop Conditions

Stop and report exact missing inputs when:

- A required upstream stage is missing.
- A manifest stage says `complete` but required artifacts are absent.
- Artifacts exist but fail their quality gates.
- The user requests a destructive rerun without naming `patch`, `regenerate`, or `migrate`.
