# Foundry Quality Gates (Governed Runtime)

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | FRD coverage | FRD in `prior_refinery` has no `## FB-NNN` Feature Blueprint | Add `## FB-NNN` blueprint |
| 2 | ADR completeness | Major decision lacks six-section `## ADR-NNN` | Complete ADR template |
| 3 | Heading IDs | Fewer than two `## ADR-/FB-/COMP-NNN` headings | Promote IDs to markdown headings |
| 4 | Summary section | Missing `# Architecture Summary` (or System Design / Architecture Overview) | Add `architecture_summary_markdown` |
| 5 | Traceability | Blueprint lacks references to FRD IDs | Add FRD references |
| 6 | Component reuse | Duplicate capability across feature blueprints | Extract `## COMP-NNN` |
| 7 | Challenge resolution | Architectural challenge unresolved | Resolve or defer in `warnings` |
| 8 | All artifact keys | Missing any of `blueprints_markdown`, `architecture_decisions_markdown`, `architecture_summary_markdown` | Return all three keys |
| 9 | Minimum content | Combined artifacts under runtime minimums / action-plan / recall text | Expand with substantive blueprint/ADR bodies |

Record unresolved items in `warnings`.

Completion: gates pass and **all** required artifact keys are present per `runtime-contract.md`.
