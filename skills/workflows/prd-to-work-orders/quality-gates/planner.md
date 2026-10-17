# Planner Quality Gates

Run these gates after Work Orders, context graph, and execution phases are written.

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | Blueprint coverage | Component or Feature Blueprint lacks an Epic. | Create missing Epic. |
| 2 | Requirement coverage | `REQ-` or `AC-` appears in no Work Order. | Add to owning Work Order. |
| 3 | No duplicate requirements | Same `AC-` appears in multiple Work Orders. | Assign to exactly one. |
| 4 | Scope boundaries | Work Order lacks In Scope or Out of Scope. | Add specific inclusions and exclusions. |
| 5 | Requirements verbatim | FRD requirements are paraphrased. | Copy exact text. |
| 6 | E2E test specs | Requirement has no concrete test spec. | Add COV-format test. |
| 7 | Context graph complete | Work Order lacks relationship block. | Add all relationship types. |
| 8 | Graph symmetry | Depends-on and depended-on-by are asymmetric. | Fix both sides. |
| 9 | No orphan Work Orders | Work Order has no relationships. | Add dependency, dependent, or shared contract. |
| 10 | No cycles | Depends-on graph contains a cycle. | Restructure boundaries. |
| 11 | Vertical traceability | Work Order cannot trace to Epic, Blueprint, FRD, persona, and business problem. | Complete chain or remove Work Order. |
| 12 | Contract ownership | Data model or API is defined in multiple Work Orders. | Assign one owner; others reference it. |
| 13 | Phase correctness | Work Order is in same or earlier phase than dependency. | Move to later phase. |
| 14 | Executability | Fresh agent likely needs clarification to implement. | Split or add context, tests, and boundaries. |
| 15 | Local testability | No local verification path is stated. | Add unit/integration/manual verification path. |
| 16 | No placeholders | Contains placeholder or vague sections. | Replace with concrete content. |
| 17 | Manifest current | `stages.planner` missing, stale, or not complete. | Update manifest after gates pass. |
| 18 | Ownership respected | Planner modified upstream Blueprints or FRDs. | Revert or reopen owning stage. |
| 19 | Executability review complete | `docs/prd-to-work-orders/reviews/planner-review.md` is missing when Planner is run. | Run Planner executability review. |
| 20 | Agent Executability Score present | Any Work Order lacks `## Agent Executability Score`. | Add the score table and roll-up. |
| 21 | No unresolved blocking review findings | Executability review contains unresolved Blocking findings. | Split or revise Work Orders. |

## Execution Phase Rules

- Phase 1 contains Work Orders with no dependencies.
- Each later phase contains Work Orders whose dependencies are in earlier phases.
- Work Orders in the same phase can run in parallel only when they have no mutual dependencies.
- Component Epics generally precede Feature Epics that compose them.
