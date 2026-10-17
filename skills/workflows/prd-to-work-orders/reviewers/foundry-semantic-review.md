# Foundry Semantic Review

Run this review before Cross-Blueprint Consistency.

## Purpose

Challenge architecture quality beyond template completion.

## Review Questions

- Do component boundaries map cleanly to reusable capabilities?
- Does every Feature Blueprint compose components instead of duplicating them?
- Are ADR alternatives genuine rather than strawmen?
- Are NFRs reflected in architecture decisions?
- Are data models complete enough for Planner to create Work Orders?
- Are integration contracts concrete enough for tests and implementation?
- Does any decision create avoidable downstream implementation risk?
- Are open assumptions carried forward explicitly?

## Output Format

```markdown
## REVIEW-NNN: [Artifact / issue]

**Severity:** Blocking | Major | Minor
**Category:** Product thinking | Ambiguity | Traceability | Executability | Evidence | Architecture
**Finding:** [Concise finding]
**Source:** [Artifact path and section]
**Recommendation:** [Specific fix]
```

## Pass Criteria

Foundry may complete only when no Blocking findings remain unresolved and all ADR challenges have responses or revisions.
