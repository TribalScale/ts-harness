# Artifact Ownership Rules

Each stage owns its own output directory. Downstream stages may cite upstream artifacts, but they must not silently edit them.

## Ownership Map

| Owner | Owns | May read |
|---|---|---|
| Umbrella skill | `docs/prd-to-work-orders/**` | All stage outputs |
| Refinery | `docs/refinery/**` | Source PRD |
| Foundry | `docs/foundry/**` | Source PRD and `docs/refinery/**` |
| Planner | `docs/planner/**` | `docs/refinery/**` and `docs/foundry/**` |
| Validator | `docs/validator/**` | All docs and implemented code |

## Rules

- Refinery must not create technical architecture, API contracts, or data models outside `overview/technical-requirements.md`.
- Foundry must not rewrite FRDs. If it finds a requirement defect, create `docs/foundry/foundry-issues.md` and ask to reopen Refinery.
- Planner must not rewrite Blueprints or FRDs. If it finds a design defect, create `docs/planner/planner-issues.md` and ask to reopen the owning stage.
- Validator must not directly fix code or specs. It creates drift reports, CWOs, or major-drift decisions.
- The umbrella skill is the only owner of the global manifest, approvals, metrics, run log, and archive.

## Rerun Boundaries

Owned artifacts can be replaced only through the rerun policy:

- `resume` keeps existing artifacts and completes missing work.
- `patch` updates artifacts while preserving stable IDs.
- `regenerate` archives and recreates a stage output.
- `migrate` upgrades artifact format without changing product intent.

## Quality Gate

Every stage must pass this ownership gate:

> No file outside the stage's owned output directory was modified unless the user explicitly approved a cross-stage migration.
