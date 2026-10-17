# Agent Executability Score Template

Use this section in every Work Order.

```markdown
## Agent Executability Score

| Dimension | Score | Evidence |
|---|---:|---|
| Independence | 3 | [No hidden dependencies beyond context graph.] |
| Context sufficiency | 3 | [FRDs, Blueprints, and contracts are linked.] |
| Local testability | 2 | [Verification path is available but may require manual step.] |
| Scope clarity | 3 | [In Scope and Out of Scope are concrete.] |
| Agent risk | 2 | [Likely clarification risks are documented.] |

**Roll-up:** 2.6 — Ready
```

## Dimensions

- **Independence:** Hidden dependencies, required prior work, ownership clarity.
- **Context sufficiency:** Linked FRDs, Blueprints, contracts, examples.
- **Local testability:** Explicit command or manual verification path.
- **Scope clarity:** Clear in-scope/out-of-scope, no bundled unrelated work.
- **Agent risk:** Likelihood a fresh coding agent asks clarification.

## Scoring

- 1 = weak
- 2 = acceptable
- 3 = strong

## Roll-Up

- `>= 2.5`: Ready
- `2.0 - 2.49`: Needs review
- `< 2.0`: Blocked
