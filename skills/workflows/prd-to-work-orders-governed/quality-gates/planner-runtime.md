# Planner Quality Gates (Governed Runtime)

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | Verbatim requirements | WO requirements differ from FRD | Copy verbatim from `prior_refinery` |
| 2 | Independence | WO has hidden undeclared dependencies | Add to context graph |
| 3 | Scope clarity | Missing In Scope / Out of Scope | Add both sections |
| 4 | E2E tests | No Given/When/Then acceptance tests | Add E2E test specs |
| 5 | Context graph | WO missing dependency links | Update `context_graph_markdown` |
| 6 | Executability | WO too large for one agent session | Split into smaller WOs |
| 7 | ID conventions | WO IDs not `WO-NNN` format | Rename to `WO-001`, etc. |
| 8 | Artifact presence | No `work_orders_markdown` | Produce work order content |
| 9 | work_order_ids | Top-level IDs missing or mismatched | Set `work_order_ids` array |
| 10 | Technical constraints | A WO lacks `## Technical Constraints`, or it omits a framework, storage, or named entity the PRD states | Copy the full constraint list into every WO |
| 11 | Repositories | With `input.project_repositories`, a WO lacks `Repository: owner/name`, names a repository not in the list, or WO dependencies form a cycle between repositories | Name one listed repository per WO; split or reorder WOs so dependencies between repositories point one way |

Record unresolved items in `warnings`. Set `planner_complete: true` only when gates pass.

Completion: gates pass, `work_order_ids` populated, required artifact keys present.
