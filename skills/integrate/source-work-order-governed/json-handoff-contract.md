# JSON Handoff Contract

## prepare_work_order_delivery Output

### Required top-level fields

| Field | Type | Rule |
|---|---|---|
| `summary` | string | Non-empty; describes handoff prepared |
| `planner_complete` | boolean | Must be `true` |
| `work_order_ids` | string[] | All `WO-NNN` ids from planner |
| `next_work_order_id` | string | First deliverable WO (e.g. `WO-001`) |
| `work_orders_handoff` | boolean | Must be `true` |
| `work_order_ref` | string | Same as `next_work_order_id` unless overridden |
| `confidence` | string | `high`, `medium`, or `low` |
| `warnings` | string[] | Dependency blockers or gaps |
| `follow_up_needed` | boolean | `true` if no WO is ready |

### Selection algorithm for next_work_order_id

1. Parse `execution_phases_markdown` for phase order.
2. Within first phase, pick lowest `WO-NNN` not in `completed_work_order_ids`.
3. Verify dependencies from `context_graph_markdown` are satisfied.
4. If none ready, set `follow_up_needed: true` and explain in `warnings`.

### Anti-patterns (blocked by runtime validation)

- Missing `next_work_order_id`
- `work_orders_handoff` not `true`
- Empty `work_order_ids`
- Action plan without handoff fields

## implement_work_orders Consumption

Program artifacts after `prepare_work_order_delivery` completes:

- `planner_complete`
- `work_order_ids`
- `next_work_order_id`
- `work_orders_handoff`
- `work_order_ref`

`implement_work_orders` intake uses `$workflow.work_order_id` and `$workflow.work_order_ref`
resolved from program artifacts.
