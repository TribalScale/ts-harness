---
name: source-work-order
description: Source adapter that fetches Work Orders from docs/planner and maps them to NormalizedDeliveryContext for verified-merge. Use when input is WO-###, CWO-###, or a path under docs/planner, or when delivery.source is work-order. Handles dependency checks and optional WO status mirroring on lifecycle events.
type: skill
disable-model-invocation: true
---

# Source: Work Order

Repo-native source adapter for `verified-merge`. Reads Work Orders from the Planner output, maps them to NormalizedDeliveryContext, and optionally mirrors status on lifecycle events.

## Adapter Contract

Implements the contract in `skills/workflows/verified-merge/adapter-contract.md`:

- `detect(input)` — matches `WO-###`, `CWO-###`, or paths under `docs/planner/**`
- `fetch(id|ref)` — reads Work Order file, context graph, execution phases, linked FRDs/Blueprints
- `on_event(event, payload)` — optional WO status mirror; reads delivery state written by orchestrator
- `capabilities()` — `{ canTransition: false, canComment: false, canLink: true, canUpdateStatus: true }`

## detect(input)

Return true when input matches:

- Work Order id pattern: `WO-[0-9]{3}` or `CWO-[0-9]{3}`
- File path under `docs/planner/`
- User explicitly requests Work Order delivery

Return false for Jira keys and unrelated inputs.

## fetch(id|ref)

### Required reads

Before mapping, read:

- The Work Order file: `docs/planner/epics/**/WO-*.md` or `docs/validator/corrective-work-orders/CWO-*.md`
- `docs/planner/context-graph.md`
- `docs/planner/execution-phases.md`
- Linked FRD and Blueprint files referenced in the Work Order

If the Work Order file cannot be found, stop and report the missing path.

### Dependency gate

From the Work Order context graph `Depends on` links:

1. Read `docs/verified-merge/delivery.json` for dependency status.
2. If a dependency unit is not `Done`, stop and report blocking ids.
3. Do not proceed to implementation until dependencies are resolved or the user explicitly overrides.

### Mapping rules

Map Work Order sections to NormalizedDeliveryContext:

| Work Order section | Context field |
|---|---|
| Title | `title` |
| Summary | `description` |
| Requirements (verbatim) | `requirements[]` |
| E2E Acceptance Tests | `acceptanceCriteria[]` (Given/When/Then form) |
| In Scope / Out of Scope | `scopeBoundaries` |
| Context Graph Depends on | `dependencies[]` |
| WO id | `source.id` |
| File path | `source.ref` |
| `source.type` | `work-order` |
| Branch from id + title slug | `branchHint` |

Set `externalRefs.jira` only when the Work Order or delivery.json already contains a Jira reference. Do not invent external refs.

## on_event(event, payload)

The orchestrator owns writes to `docs/verified-merge/delivery.json`. This adapter:

1. Reads the current delivery state for the unit.
2. Optionally mirrors status to the Work Order markdown file when `mirror_status_to_work_order: true`.

### Status mirror mapping (when enabled)

| Event | WO Status field |
|---|---|
| `context_accepted` or `in_development` | In Progress |
| `pr_opened` | In Review |
| `post_merge_verified` | Done |

When mirroring is disabled (default), do not modify `docs/planner/**`.

Never edit `docs/prd-to-work-orders/manifest.json`.

## capabilities()

```yaml
canTransition: false
canComment: false
canLink: true
canUpdateStatus: true
```

Repo-native status lives in `delivery.json`. External tracker sync is not this adapter's job.

## When to Use

Invoked by `verified-merge` when:

- Input is a Work Order id or planner path.
- Configured `delivery.source` is `work-order`.
- No other adapter matches input more specifically.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Editing upstream manifest | Only read planner artifacts; delivery state is in delivery.json |
| Ignoring dependencies | Check context graph and delivery.json before fetch completes |
| Inventing requirements | Copy Requirements verbatim from Work Order |
| Mirroring WO status by default | Mirror only when config enables it |

## Additional Resources

- Adapter contract: see `verified-merge` workflow `adapter-contract.md`
- Pressure scenarios: [test-scenarios.md](test-scenarios.md)
