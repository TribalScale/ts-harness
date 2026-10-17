# Distribution Strategy

`skills/workflows/prd-to-work-orders/` is the canonical skill package. It is maintained in this repository and shipped in the runtime image.

The four pipeline stages (Refinery, Foundry, Planner, Validator) are internal modules orchestrated by the umbrella workflow. Do not split them into separate installable skills.

## Distribution Targets

| Target | Output | Status |
|---|---|---|
| Runtime skills catalog | `skills/workflows/prd-to-work-orders/**` | Canonical |
| Cursor | `~/.cursor/skills/prd-to-work-orders/` | Copy the folder |
| Claude Code | `~/.claude/skills/prd-to-work-orders/` | Copy the folder |

## Installation Rule

Copy the full workflow folder into the target environment's skill directory. Do not copy individual stage files.

## Drift Check

Before release:

- Confirm `name` in `SKILL.md` matches the directory name (`prd-to-work-orders`).
- Run `python -m pytest tests/runtime/test_skills.py -q`.
- Confirm fixtures and examples still reference the canonical skill name.
