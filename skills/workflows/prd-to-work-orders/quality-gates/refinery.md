# Refinery Quality Gates

Run these gates before marking Refinery complete.

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | User-centered | AC describes internal mechanics instead of user-visible behavior. | Rewrite from the persona's perspective. |
| 2 | Testable | AC cannot be tested with setup, action, assertion. | Add a concrete trigger and observable behavior. |
| 3 | Atomic | AC covers multiple behaviors joined by `and` or `or`. | Split into separate ACs. |
| 4 | No vagueness | Uses "appropriate", "properly", "as needed", "etc.", "handle errors", "relevant", or "seamless". | Replace with specific behavior. |
| 5 | Terminology consistent | A term has conflicting definitions or is undefined across FRDs. | Resolve in shared terminology index. |
| 6 | Implementation-agnostic | FRD prescribes APIs, databases, frameworks, queues, protocols, or libraries. | Move technical content to Technical Requirements or Foundry handoff. |
| 7 | Role specificity | Uses generic "user" when a persona applies. | Replace with the persona name. |
| 8 | Outcome clarity | User story lacks a meaningful "so that" outcome. | State the user value or business outcome. |
| 9 | Dependency correctness | Missing or circular dependencies in feature tree. | Fix dependency graph before FRDs proceed. |
| 10 | PRD coverage | Requirements-bearing PRD section maps to no FRD. | Add or update an FRD. |
| 11 | Gap disposition | High-impact gap lacks resolved/deferred/approved status. | Record decision in assumptions and approvals. |
| 12 | Manifest current | `stages.refinery` missing, stale, or not complete. | Update manifest after gates pass. |
| 13 | Ownership respected | Refinery modified files outside `docs/refinery/**` or global manifest/approvals/metrics. | Revert or get explicit approval. |
| 14 | Semantic review complete | `docs/prd-to-work-orders/reviews/refinery-review.md` is missing when Refinery is run. | Run the Refinery semantic review. |
| 15 | No unresolved blocking review findings | Semantic review contains unresolved Blocking findings. | Resolve or explicitly defer with owner/risk. |

## Completion Criteria

Refinery is complete only when all gates pass and `docs/refinery/phase-5-cross-frd-consistency.md` records terminology alignment, conflict check, dependency completeness, upward traceability, and full PRD coverage.
