# Feedback Ingestion Template

```markdown
# Feedback Ingestion — YYYY-MM-DD

## Source

**Type:** User feedback | Test results | QA notes | Production incident
**Scope:** [Feature, Work Order, or area]

## Feedback Items

### FB-NNN: [Title]

**Type:** Bug | Spec gap | Enhancement | Test failure
**Observed in:** [Screen, API, test file, or workflow]
**Description:** [What happened]
**Expected:** [Expected behavior]
**Mapped spec:** [FRD / Blueprint / Work Order]
**Classification:** Implementation gap | Spec gap | Enhancement
**Action:** CWO-NNN | Escalate | Log for backlog
```

## Rules

- Group test failures by root cause.
- Enhancements do not generate CWOs.
- Spec gaps require human decision.
- Implementation gaps trace to a concrete FRD, Blueprint, or Work Order.
