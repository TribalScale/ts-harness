---
name: prd-to-work-orders-governed
description: Governed-runtime PRD-to-Work-Orders for enterprise workflow dispatch (JSON artifacts, no manifest/filesystem).
type: workflow
category: governed-workflows
allowed_agents: [pm-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.0.0
---

# PRD to Work Orders (Governed Runtime)

`prd-to-work-orders-governed` runs the Refinery → Foundry → Planner pipeline inside
enterprise workflow dispatch. Each stage is a discrete workflow task with durable JSON
input/output — not filesystem artifacts.

Use the filesystem skill (`prd-to-work-orders`) for local/Cursor runs with
`docs/prd-to-work-orders/manifest.json`. Use this skill for governed EC2 delivery.

## Start Here

At invocation, announce:

> "I'm using the governed prd-to-work-orders workflow to produce stage artifacts
> for input.stage."

Then run these checks in order:

1. Read `input.stage` — this is the **only** stage selector (`refinery`, `foundry`, or `planner`).
2. Read `input.approved_prd` as the source PRD.
3. Read optional `input.prior_refinery` and `input.prior_foundry` when present.
4. Load the matching stage module from `stages/<stage>-runtime.md`.
5. Load the matching quality gate from `quality-gates/<stage>-runtime.md`.
6. Execute exactly one stage and return JSON per `output-envelope.md`.

Never infer stage from user wording, manifest, or workspace files.
Never require or inspect `docs/prd-to-work-orders/manifest.json`, `state-machine.md`,
or files under `docs/refinery`, `docs/foundry`, or `docs/planner`.
Do not stop because filesystem modules are missing.

## Mandatory Reads (inlined)

- `runtime-contract.md` — JSON I/O schema
- `stage-routing.md` — stage → module mapping
- `output-envelope.md` — required response shape
- `stages/<stage>-runtime.md` for the pinned stage
- `quality-gates/<stage>-runtime.md` for the pinned stage

## Stage Routing

| `input.stage` | Module | Purpose |
|---|---|---|
| `refinery` | `stages/refinery-runtime.md` | PRD → requirements, FRDs, feature tree |
| `foundry` | `stages/foundry-runtime.md` | Refinery output → blueprints, ADRs |
| `planner` | `stages/planner-runtime.md` | Foundry output → work orders, context graph |

Validator is **not** part of governed dispatch. It remains a local/off-critical-path stage.

## Output Contract

Return JSON with:

- `summary` — one-paragraph stage completion summary
- `artifacts` — markdown artifacts keyed by stage (see `runtime-contract.md`)
- `confidence` — `high`, `medium`, or `low`
- `warnings` — array of strings (empty if none)
- `follow_up_needed` — boolean

For `planner`, also set top-level `work_order_ids` (array of `WO-NNN` strings) when known.

Do not return an action plan or next-step list as the artifact.
Return the completed stage content itself in `artifacts`.

## Global Rules

- Use `input.approved_prd` and prior-stage JSON as source of truth.
- Preserve ID conventions: `FRD-NNN`, `FB-NNN`, `WO-NNN`, `CE-NNN`, `FE-NNN`.
- Acceptance criteria: `When [condition], the system shall [behavior]`.
- Downstream stages must not silently edit upstream artifact content; cite prior JSON.
- Apply quality-gate checklist before returning; record gate failures in `warnings`.

## Parent Skill Reference

Templates, schemas, and detailed examples live in `prd-to-work-orders`.
See `references/parent-skill.md` for paths. Do not require those files at runtime.
