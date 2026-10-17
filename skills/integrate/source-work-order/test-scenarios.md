# source-work-order Test Scenarios

Pressure-test `source-work-order` before relying on the adapter or after editing it.

## Scenario 1: Fetch Happy Path

Prompt:

```text
Fetch WO-003 for delivery.
```

Expected:

- Reads `docs/planner/epics/**/WO-003*.md`, context graph, execution phases.
- Maps requirements verbatim, acceptance criteria from E2E tests, dependencies from context graph.
- Returns NormalizedDeliveryContext with `source.type: work-order`.

Failure:

- Invents requirements not in the Work Order file.
- Skips context graph read.

## Scenario 2: Missing Work Order

Prompt:

```text
Fetch WO-999.
```

Expected:

- Reports Work Order not found.
- Does not produce a fabricated context.

## Scenario 3: Blocking Dependency

Prompt:

```text
Fetch WO-005. Depends on WO-004 which is not Done in delivery.json.
```

Expected:

- Reports WO-004 as blocking.
- Does not return a context ready for implementation.

Failure:

- Proceeds despite unresolved dependency.

## Scenario 4: Mirror Disabled

Config: `mirror_status_to_work_order: false`

Event: `pr_opened`

Expected:

- Does not modify `docs/planner/**/WO-*.md`.

## Scenario 5: Mirror Enabled

Config: `mirror_status_to_work_order: true`

Event: `post_merge_verified`

Expected:

- Updates Work Order Status field to Done.
- Does not edit manifest.json.

## Scenario 6: Detect Routing

Inputs and expected detect results:

| Input | detect |
|---|---|
| `WO-003` | true |
| `docs/planner/epics/FE-001/WO-003-export.md` | true |
| `PROJ-123` | false |

## Regression Checklist

- detect matches WO/CWO ids and planner paths only.
- fetch maps all contract fields from Work Order artifacts.
- dependencies gate on delivery.json status.
- on_event respects mirror config.
- never writes to docs/prd-to-work-orders/manifest.json.
