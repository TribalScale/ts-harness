# Rerun and Regeneration Policy

Use this policy whenever an output directory already exists, an upstream source changed, or the user asks to "redo" a stage.

## Modes

| Mode | Use when | Behavior |
|---|---|---|
| `resume` | A stage is incomplete and existing artifacts are still valid. | Continue without deleting or renumbering existing artifacts. |
| `patch` | Upstream changes are small and stable IDs can be preserved. | Update affected artifacts and write a stage change report. |
| `regenerate` | Existing artifacts are wrong, obsolete, or structurally unusable. | Archive existing output, then recreate the stage from upstream inputs. |
| `migrate` | Artifact schema changed but product intent is unchanged. | Upgrade structure and record migration lineage. |

## Staleness Detection

Compare hashes in `docs/prd-to-work-orders/manifest.json` with current source files.

- PRD changed after Refinery: mark Refinery, Foundry, Planner, and Validator stale.
- FRDs changed after Foundry: mark Foundry, Planner, and Validator stale.
- Blueprints changed after Planner: mark Planner and Validator stale.
- Work Orders changed after implementation: Validator must record whether code targeted the old or new Work Order version.

## Archive Policy

Before `regenerate`, move the existing stage output to:

```text
docs/prd-to-work-orders/archive/<timestamp>/<stage>/
```

Before `patch`, write:

```text
docs/<stage>/stage-change-report.md
```

The change report lists:

- Source files changed
- Artifact IDs added
- Artifact IDs modified
- Artifact IDs deprecated
- Downstream stages affected
- Recommended next action

## ID Stability

- `patch` preserves existing IDs whenever intent remains the same.
- Removed items become deprecated; IDs are not reused.
- `regenerate` may renumber only with user approval and a migration report.
- `migrate` must preserve IDs.

## User Confirmation

Ask for explicit approval before:

- Deleting or archiving current stage output.
- Renumbering IDs.
- Marking downstream implementation as stale.
- Migrating an incompatible schema version.
