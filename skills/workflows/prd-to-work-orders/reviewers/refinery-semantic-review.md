# Refinery Semantic Review

Run this review before marking Refinery complete.

## Purpose

Challenge whether the requirements are good product thinking, not just well-formatted FRDs.

## Review Questions

- Does every feature connect to a real persona goal and business problem?
- Are any requirements implementation details disguised as product behavior?
- Are personas specific enough to make prioritization decisions?
- Are success metrics relevant to user or business outcomes?
- Are gaps resolved, deferred with owner/risk, or blocking?
- Are any features over-scoped for the stated V1?
- Are any important user paths missing?
- Are assumptions visible in the manifest and working assumptions file?

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

Refinery may complete only when no Blocking findings remain unresolved.
