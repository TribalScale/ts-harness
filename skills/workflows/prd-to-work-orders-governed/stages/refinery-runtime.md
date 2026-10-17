# Stage: Refinery (Governed Runtime)

Transform `input.approved_prd` into structured, implementation-agnostic requirements
returned as JSON markdown artifacts.

## Inputs

- `input.approved_prd` — PRD object (`summary`, `prd_markdown`, or equivalent)
- `input.stage` — must be `refinery`

Do not read manifest or filesystem paths.

## Outputs (JSON artifacts)

Return under `output.artifacts`:

| Key | Content |
|---|---|
| `product_overview_markdown` | Business problem, current state, personas, product description, success metrics |
| `requirements_markdown` | Technical requirements and consolidated requirements |
| `feature_tree_markdown` | Feature tree with dependencies and complexity |
| `frds_markdown` | All FRDs (`# FRD-001 Feature Name` sections) |

## Phases

1. **PRD analysis** — separate requirements from technical content; identify gaps and assumptions.
2. **Product overview** — business problem, personas, product description, success metrics.
3. **Feature decomposition** — feature tree with dependencies.
4. **FRD authoring** — one FRD per feature in dependency order.
5. **Cross-FRD consistency** — terminology, traceability, coverage check (record in `warnings` if issues remain).

## Rules

- FRDs describe what and why, never how.
- Acceptance criteria: `When [condition], the system shall [behavior]`.
- Every requirement traces to a persona and business problem.
- Open assumptions use `ASM-*` or `DEFER:*` markers in `warnings` or artifact body.
- Do not prescribe APIs, databases, or frameworks in FRDs.

## FRD Template (inline)

Each FRD section in `frds_markdown`:

```markdown
# FRD-001 Feature Name

## Overview
...

## Personas
...

## User Stories
As a [persona], I want [goal] so that [outcome].

## Requirements
- REQ-XXX-001: ...

## Acceptance Criteria
- When [condition], the system shall [behavior].

## Dependencies
- FRD-002
```

Apply `quality-gates/refinery-runtime.md` before returning JSON.
