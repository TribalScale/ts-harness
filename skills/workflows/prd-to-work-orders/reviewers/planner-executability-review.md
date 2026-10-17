# Planner Executability Review

Run this review before execution phase approval.

## Purpose

Determine whether a fresh coding agent can execute each Work Order without asking for missing product, architecture, or testing context.

## Review Questions

- Is the Work Order independently implementable?
- Are hidden dependencies listed in the context graph?
- Are the related FRDs and Blueprints specific enough to find the required context?
- Are In Scope and Out of Scope concrete and non-overlapping?
- Is there a local verification path?
- Are E2E acceptance tests specific enough to implement or manually verify?
- Does the Agent Executability Score match the actual Work Order quality?
- Is the Work Order too large or bundled across unrelated concerns?

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

Planner may complete only when no Blocking executability findings remain unresolved and every Work Order has an Agent Executability Score.
