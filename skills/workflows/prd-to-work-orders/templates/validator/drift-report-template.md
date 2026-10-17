# Drift Report Template

```markdown
# Drift Report: [Checkpoint or Final Review]

**Date:** YYYY-MM-DD
**Scope:** [Epic, Phase, Work Order, or Full codebase]
**Phase:** [Phase]
**Status:** Clean | Minor drift | Major drift | Mixed drift

## Drift Items

### DRIFT-NNN: [Short description]

**Severity:** Minor | Major
**Category:** Data model | Contract | AC coverage | Boundary | Naming | ADR | Spec drift | Intent drift

**Expected (from specs):**
- **Source:** [Blueprint section, FRD requirement, or Work Order]
- **Specification says:** [Exact quote or precise summary]

**Actual (in code):**
- **File:** [path and line or symbol]
- **Code does:** [Precise behavior]

**Impact:**
[What breaks or degrades.]

**Corrective action:**
- [ ] CWO-NNN generated, or
- [ ] Escalated to human with fix-code/update-spec options

## Summary

| Severity | Count |
|---|---:|
| Minor | 0 |
| Major | 0 |
| **Total** | **0** |
```
