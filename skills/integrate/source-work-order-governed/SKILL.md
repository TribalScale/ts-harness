---
name: source-work-order-governed
description: Governed-runtime source adapter that reads Work Orders from planner JSON artifacts and maps them to NormalizedDeliveryContext for implement handoff.
type: skill
category: governed-workflows
allowed_agents: [orchestrator]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.0.0
---

# Source: Work Order (Governed Runtime)

JSON handoff adapter for governed delivery. Reads Work Orders from planner task
output JSON — not from `docs/planner/**` files.

Use the filesystem adapter (`source-work-order`) for local runs with on-disk planner output.

## When to Run

Workflow step: `prepare_work_order_delivery` in `work_orders_from_prd`.

## Inputs

```json
{
  "approved_prd": { "...": "..." },
  "planner_output": { "summary": "...", "artifacts": { ... }, "work_order_ids": [...] },
  "planner_artifacts": {
    "work_orders_markdown": "# WO-001 ...",
    "context_graph_markdown": "# Context Graph ...",
    "execution_phases_markdown": "# Execution Phases ..."
  }
}
```

## Required Reads

1. `input.planner_artifacts.work_orders_markdown`
2. `input.planner_artifacts.context_graph_markdown`
3. `input.planner_artifacts.execution_phases_markdown`
4. `input.planner_output.work_order_ids` if present

If planner artifacts are missing, stop and report in JSON `warnings` — do not look for filesystem paths.

## Output Contract

Return JSON:

```json
{
  "summary": "Prepared WO-001 for implement handoff.",
  "planner_complete": true,
  "work_order_ids": ["WO-001", "WO-002"],
  "next_work_order_id": "WO-001",
  "work_orders_handoff": true,
  "work_order_ref": "WO-001",
  "confidence": "high",
  "warnings": [],
  "follow_up_needed": false
}
```

See `json-handoff-contract.md` for field rules.

## Dependency Gate

From `context_graph_markdown` Depends-on links:

1. Read `input.completed_work_order_ids` if provided (program artifacts).
2. If a dependency WO is not in completed list, set `follow_up_needed: true` and report blockers in `warnings`.
3. Select `next_work_order_id` as the first WO in `execution_phases_markdown` order that is not completed and has satisfied dependencies.

## Mapping Rules

| Planner content | Output field |
|---|---|
| First ready WO id | `next_work_order_id` |
| All WO ids | `work_order_ids` |
| WO id string | `work_order_ref` |
| Handoff ready | `work_orders_handoff: true` |
| Planner done | `planner_complete: true` |

Do not return an action plan. Return the handoff fields above.
