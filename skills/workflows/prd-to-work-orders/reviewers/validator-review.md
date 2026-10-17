# Validator Review

Run this review before finalizing a drift report.

## Purpose

Prevent false positives and weak corrective Work Orders.

## Review Questions

- Does every drift item cite both code evidence and spec evidence?
- Is the drift classification correct: implementation drift, spec drift, or intent drift?
- Are minor items mechanical and major items truly architectural or behavioral?
- Could the code satisfy the requirement through a different but valid approach?
- Does each CWO map to one root cause?
- Are enhancements separated from implementation gaps?
- Are major drift decisions presented with fix-code and update-spec options?
- Does the report scope match what was actually reviewed?

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

Validator may complete only when no Blocking evidence or false-positive concerns remain unresolved.
