# Evaluation Metrics

Production maturity means the skill improves delivery quality, not just document volume. Record metrics at stage completion in `docs/prd-to-work-orders/metrics.md`.

## Core Metrics

| Metric | Definition | Stage |
|---|---|---|
| PRD-to-FRD coverage | Requirements-bearing PRD sections mapped to one or more FRDs / total requirements-bearing sections. | Refinery |
| AC testability | ACs with concrete setup, action, and assertion / total ACs. | Refinery |
| Clarification load | Count of `CLAR-NNN` records by stage. | All |
| Gate failure density | Failed gates / artifact count. | All |
| Work Order executability | Work Orders marked Ready by Agent Executability Score and completed without clarification / completed Work Orders. | Planner/Implementation |
| Drift rate | Drift items / completed Work Orders. | Validator |
| Rework rate | Patched or regenerated artifacts / total artifacts. | All |
| Time to first Work Order | Elapsed time from accepted PRD to first Planner-ready Work Order. | Pipeline |

## Metrics Template

```markdown
# PRD to Work Orders Metrics

## Summary

| Metric | Value | Baseline | Status |
|---|---:|---:|---|
| PRD-to-FRD coverage | 0% | Not measured | Pending |
| AC testability | 0% | Not measured | Pending |
| Clarification load | 0 | Not measured | Pending |
| Gate failure density | 0 | Not measured | Pending |
| Work Order executability | 0% | Not measured | Pending |
| Drift rate | 0 | Not measured | Pending |
| Rework rate | 0% | Not measured | Pending |
| Time to first Work Order | 0 | Not measured | Pending |

## Stage Notes

[Record what drove the numbers and any changes needed.]
```

## Regression Use

The golden path example is the baseline. Regression fixture runs compare current output against:

- Required artifact structure
- Gate pass/fail results
- Metric deltas
- Known intentional drift findings

Flag a regression when a fixture loses traceability, reduces AC coverage, emits more unexplained drift, or requires more clarification than the baseline.

## Clarification Tracking

Clarifications are recorded in `docs/prd-to-work-orders/clarifications.md`.

Use clarification counts to improve templates and gates:

- Repeated missing-context questions indicate weak Work Order context.
- Repeated scope-boundary questions indicate unclear In Scope / Out of Scope sections.
- Repeated missing-contract questions indicate weak Foundry Blueprints.
- Repeated external-blocker questions indicate assumptions need owners and review triggers.
