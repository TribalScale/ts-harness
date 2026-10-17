# Output Envelope

Every governed stage must return this JSON shape.

## Required Fields

```json
{
  "summary": "One paragraph describing what was produced.",
  "artifacts": {
    "artifact_key_markdown": "# Full markdown content..."
  },
  "confidence": "high",
  "warnings": [],
  "follow_up_needed": false
}
```

## Field Rules

| Field | Rule |
|---|---|
| `summary` | Non-empty string; describes completed work, not next steps |
| `artifacts` | Object with at least one stage-specific `*_markdown` key |
| `confidence` | `high`, `medium`, or `low` |
| `warnings` | Array of strings; empty array if none |
| `follow_up_needed` | `true` only if a human blocker remains |

## Planner Extras

When `input.stage = planner`, also include top-level:

```json
{
  "work_order_ids": ["WO-001", "WO-002"],
  "planner_complete": true
}
```

## Anti-patterns (will be blocked by runtime validation)

- Action plan only: numbered steps without `artifacts` body
- Empty `artifacts` object
- Missing stage keys (refinery/foundry/planner each require their full key set)
- Foundry IDs only in bullets (`- **FB-001**`) without `## FB-001` / `## ADR-001` headings
- Foundry missing `# Architecture Summary`
- Placeholder text: "TBD", "to be determined", "see manifest"
- Referencing missing filesystem paths as blockers
- Memory-recall / catalog summaries instead of artifact bodies
