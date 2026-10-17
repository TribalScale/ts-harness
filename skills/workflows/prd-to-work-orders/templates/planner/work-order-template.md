# Work Order Template

```markdown
# WO-NNN: [Work Order Title]

**Epic:** [CE-NNN or FE-NNN]: [Epic Name]
**Status:** Backlog | Ready | In Progress | In Review | Done
**Complexity:** S | M | L
**Phase:** [Phase number]

## Context Graph

| Relationship | Work Orders |
|---|---|
| **Depends on** | [WO-NNN or None] |
| **Depended on by** | [WO-NNN or None] |
| **Shares contract with** | [WO-NNN or None] |
| **Related blueprints** | [Blueprint names] |
| **Related FRDs** | [FRD-NNN] |

## Summary

[What this Work Order builds and what outcome it enables.]

## Agent Executability Score

| Dimension | Score | Evidence |
|---|---:|---|
| Independence | 3 | [No hidden dependencies beyond context graph.] |
| Context sufficiency | 3 | [FRDs, Blueprints, and contracts are linked.] |
| Local testability | 2 | [Verification path is available but may require manual step.] |
| Scope clarity | 3 | [In Scope and Out of Scope are concrete.] |
| Agent risk | 2 | [Likely clarification risks are documented.] |

**Roll-up:** 2.6 — Ready

## In Scope

- [Specific deliverable]

## Out of Scope

- [Explicit exclusion]

## Requirements

[Copy FRD requirements verbatim.]

## Blueprints

- **[Blueprint]** — [What it covers]

## E2E Acceptance Tests

### COV_[PREFIX]_[NAME]

1. [Precondition]
2. [Action]
3. Assert [expected outcome]

## Subtasks

- [ ] **WO-NNN.1:** [Implementation step]
```

## Rules

- Requirements are verbatim.
- In Scope and Out of Scope are mandatory.
- Every Work Order has context graph links.
- Every Work Order includes a local verification path.
