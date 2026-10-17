# Stage Routing (Governed Runtime)

The workflow engine pins the stage via `input.stage`. The agent must not infer stage
from user wording, manifest, or filesystem presence.

## Routing Table

| `input.stage` | Stage module | Quality gate | Prior inputs |
|---|---|---|---|
| `refinery` | `stages/refinery-runtime.md` | `quality-gates/refinery-runtime.md` | `approved_prd` |
| `foundry` | `stages/foundry-runtime.md` | `quality-gates/foundry-runtime.md` | `approved_prd`, `prior_refinery` |
| `planner` | `stages/planner-runtime.md` | `quality-gates/planner-runtime.md` | `approved_prd`, `prior_refinery`, `prior_foundry` |

## Workflow Step Mapping

| Workflow step | `input.stage` |
|---|---|
| `refine_requirements` | `refinery` |
| `design_blueprints` | `foundry` |
| `plan_work_orders` | `planner` |

Human approval gates (`approve_refinery`, `approve_foundry`, `approve_work_orders`)
run on the orchestrator — not on this skill.

## Invalid States

If `input.stage` is missing or not one of `refinery`, `foundry`, `planner`:

- Return JSON with `follow_up_needed: true`
- Put the error in `warnings`
- Do not attempt filesystem state detection
