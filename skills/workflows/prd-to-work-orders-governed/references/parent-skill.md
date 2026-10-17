# Parent Skill Reference

The governed runtime variant reuses content conventions from `prd-to-work-orders`.
Do not require these files at dispatch — they are for maintainers and local runs.

## Parent skill location

- TS Harness: `skills/workflows/prd-to-work-orders/`
- Skills library: `skills/workflows/prd-to-work-orders/`

## Reused assets (reference only)

| Asset | Path |
|---|---|
| FRD schema | `schemas/frd.schema.json` |
| Blueprint schema | `schemas/blueprint.schema.json` |
| Work order schema | `schemas/work-order.schema.json` |
| FRD template | `templates/refinery/frd-template.md` |
| Feature tree template | `templates/refinery/feature-tree-template.md` |
| Work order template | `templates/planner/work-order-template.md` |
| Context graph template | `templates/planner/context-graph-template.md` |
| ADR template | `templates/foundry/adr-template.md` |

## When to use which skill

| Environment | Skill |
|---|---|
| Cursor/local with filesystem | `prd-to-work-orders` |
| EC2 governed workflow dispatch | `prd-to-work-orders-governed` |
