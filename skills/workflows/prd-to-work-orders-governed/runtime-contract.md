# Runtime Contract

Authoritative JSON input/output schema for governed workflow dispatch.

## Task Input (from workflow runner)

```json
{
  "approved_prd": { "summary": "...", "prd_markdown": "...", "title": "..." },
  "stage": "refinery | foundry | planner",
  "prior_refinery": { "requirements_markdown": "...", "frds_markdown": "...", ... },
  "prior_foundry": { "blueprints_markdown": "...", "architecture_decisions_markdown": "...", ... }
}
```

- `approved_prd` — required on all planning stages
- `stage` — required; pins the stage (never infer from disk)
- `prior_refinery` — required on `foundry` and `planner` when available
- `prior_foundry` — required on `planner` when available

## Task Output Envelope

```json
{
  "summary": "Refinery complete for online checkers PRD.",
  "artifacts": {
    "<stage-specific-key>": "# Markdown content..."
  },
  "confidence": "high",
  "warnings": [],
  "follow_up_needed": false
}
```

## Artifact Keys by Stage

### Refinery (`input.stage = refinery`)

**All four keys required** (runtime validation blocks otherwise):

| Key | Content |
|---|---|
| `product_overview_markdown` | Business problem, personas, product description, success metrics |
| `requirements_markdown` | Consolidated requirements and technical requirements |
| `feature_tree_markdown` | Feature tree with dependencies (substantive; not a 3-bullet stub) |
| `frds_markdown` | At least **two** FRDs with `## FRD-NNN` (or `# FRD-NNN`) headings |

Keys must not share identical bodies. Thin single-FRD packs are rejected.

### Foundry (`input.stage = foundry`)

**All three keys required**:

| Key | Content |
|---|---|
| `blueprints_markdown` | Container/component/feature blueprints with `## COMP-/FB-NNN` headings |
| `architecture_decisions_markdown` | ADRs with six-section template and `## ADR-NNN` headings |
| `architecture_summary_markdown` | Cross-blueprint consistency under `# Architecture Summary` |

Inputs: `input.approved_prd` + `input.prior_refinery`.

### Planner (`input.stage = planner`)

**All three keys required**:

| Key | Content |
|---|---|
| `work_orders_markdown` | Work orders (`## WO-NNN`) with verbatim requirements from FRDs |
| `context_graph_markdown` | Dependency graph linking WOs, blueprints, FRDs |
| `execution_phases_markdown` | Topologically sorted execution phases |

Top-level output fields (in addition to envelope):

| Key | Content |
|---|---|
| `work_order_ids` | `["WO-001", "WO-002", ...]` |
| `planner_complete` | `true` when planner artifacts are ready |

Inputs: `input.approved_prd` + `input.prior_refinery` + `input.prior_foundry`.

## Quality Minimums

- Each artifact markdown must have at least 3 headings (`#` or `##`)
- FRDs must include user stories and acceptance criteria
- Work orders must include In Scope, Out of Scope, and E2E acceptance tests
- No action-plan-only responses (numbered "next steps" without artifact body)

## ID Conventions (unchanged from parent skill)

- FRDs: `FRD-001`, `FRD-002`, ...
- Feature blueprints: `FB-001`, ...
- Work orders: `WO-001`, `WO-002`, ...
- Epics: `CE-001` (capability), `FE-001` (feature)
