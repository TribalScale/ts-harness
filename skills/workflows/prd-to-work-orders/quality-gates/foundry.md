# Foundry Quality Gates

Run these gates after all Blueprints and adversarial challenges are written.

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | FRD coverage | Any FRD lacks a corresponding Feature Blueprint. | Write missing Feature Blueprint. |
| 2 | Requirement traceability | Requirement has no technical path through Feature Blueprint. | Add component composition and data flow. |
| 3 | Data model completeness | Model lacks fields, types, constraints, relationships, or invariants. | Complete model definition. |
| 4 | Contract completeness | API/event/integration lacks request, response, error, or payload shape. | Define full field-level contract. |
| 5 | Name consistency | Component, model, field, or event has inconsistent names. | Standardize names across Blueprints. |
| 6 | No orphan components | Component Blueprint is never composed by a Feature Blueprint. | Compose it or remove it. |
| 7 | No duplicated capabilities | Multiple Feature Blueprints define the same reusable capability. | Extract a Component Blueprint. |
| 8 | Container alignment | Component references a container without a Container Blueprint. | Create or reassign container. |
| 9 | ADR structure | ADR lacks any of the six required sections. | Replace with the hardened ADR template. |
| 10 | No placeholders | Contains placeholder, vague, or incomplete sections. | Replace with concrete content. |
| 11 | Minimum alternatives | ADR has fewer than two real alternatives besides the chosen option. | Add genuine alternatives. |
| 12 | Rejection rationale | Rejection rationale is absent or vague. | Add number, citation, or concrete scenario. |
| 13 | Quantified trade-offs | Latency or cost cells use vague language instead of values or explicit N/A. | Quantify or justify N/A. |
| 14 | Agent implementability breakdown | Missing sub-dimension table, calculation, or binding constraint. | Add full breakdown. |
| 15 | Implementability per alternative | Alternatives lack their own Agent Implementability breakdown. | Add like-for-like tables. |
| 16 | No unresolved challenges | `architectural-challenges.md` has challenge without rebuttal or revised ADR. | Resolve before consistency phase. |
| 17 | Manifest current | `stages.foundry` missing, stale, or not complete. | Update manifest after gates pass. |
| 18 | Ownership respected | Foundry modified upstream FRDs or non-owned files. | Revert or reopen owning stage. |
| 19 | Semantic review complete | `docs/prd-to-work-orders/reviews/foundry-review.md` is missing when Foundry is run. | Run Foundry semantic review. |
| 20 | No unresolved blocking review findings | Semantic review contains unresolved Blocking findings. | Resolve or explicitly defer with owner/risk. |

## Required ADR Structure

Every ADR must include:

1. Context
2. Alternatives Considered
3. Decision
4. Rejection Rationale
5. Trade-off Scorecard
6. Consequences and Downstream Impact

## Architecture Summary

After all gates pass, write `docs/foundry/architecture-summary.md` with containers, components, Feature Blueprints, shared models, and top architectural decisions.
