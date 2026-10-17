# Validator Quality Gates

Run these gates after producing drift reports, corrective Work Orders, or feedback processing output.

## Drift Report Gates

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | Evidence-based | Drift lacks both code evidence and spec evidence. | Cite code path/line or symbol and spec source. |
| 2 | No orphan drift | Drift has no CWO or major-drift escalation. | Add CWO or decision options. |
| 3 | Classification correct | Mechanical issue marked major, or architectural issue marked minor. | Reclassify with rationale. |
| 4 | No false positives | Different-but-valid implementation is reported as drift. | Remove or classify as spec drift. |
| 5 | Complete sweep | Any category is skipped: model, contract, AC coverage, boundary, naming, ADR. | Re-run missing category. |
| 6 | Summary accurate | Counts do not match drift items. | Fix summary. |
| 7 | Scope correct | Report checks outside declared scope or omits in-scope code. | Align report to scope. |

## Corrective Work Order Gates

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 8 | Template compliance | CWO lacks Summary, In Scope, Out of Scope, Requirements, Blueprints, or E2E Tests. | Fill template. |
| 9 | Context graph | CWO lacks source drift and related Work Order links. | Add context graph. |
| 10 | Minimal scope | CWO bundles multiple unrelated fixes. | Split by root cause. |
| 11 | Verbatim requirements | Mapped AC is paraphrased. | Copy exact text. |
| 12 | Test specification | No existing or new test is named. | Add verification. |

## Feedback Gates

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 13 | All feedback classified | Feedback item has no classification. | Mark spec gap, implementation gap, or enhancement. |
| 14 | Spec gaps escalated | Spec gap auto-generates a CWO without human decision. | Escalate. |
| 15 | Enhancements logged | Enhancement generates CWO as if it were bug. | Log for product backlog. |
| 16 | Deduplication | One root cause produces many CWOs. | Group by root cause. |
| 17 | Mapping verified | Implementation gap does not trace to Blueprint or FRD. | Add trace or reclassify. |

## Global Gates

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 18 | Manifest current | `stages.validator` missing, stale, or not complete. | Update manifest. |
| 19 | Ownership respected | Validator directly edits code/specs without approval. | Revert and create CWO or escalation. |
| 20 | Pipeline continuity | CWOs have no phase assignment or Planner handoff path. | Assign phase and graph links. |
| 21 | Validator review complete | `docs/prd-to-work-orders/reviews/validator-review.md` is missing when Validator is run. | Run Validator review. |
| 22 | No unresolved false-positive concerns | Validator review flags unresolved false-positive or evidence concerns. | Revise drift report or add escalation. |
