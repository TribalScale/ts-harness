# Planner Quality Gates (Governed Runtime)

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | Verbatim requirements | WO requirements differ from FRD | Copy verbatim from `prior_refinery` |
| 2 | Independence | WO has hidden undeclared dependencies | Add to context graph |
| 3 | Scope clarity | Missing In Scope / Out of Scope | Add both sections |
| 4 | E2E tests | No Given/When/Then acceptance tests | Add E2E test specs |
| 5 | Context graph | WO missing dependency links | Update `context_graph_markdown` |
| 6 | Executability | WO too large for one agent session (about 15 files or 12 requirements is the limit) | Split along a seam that lets the parts run in parallel |
| 7 | ID conventions | WO IDs not `WO-NNN` format | Rename to `WO-001`, etc. |
| 8 | Artifact presence | No `work_orders_markdown` | Produce work order content |
| 9 | work_order_ids | Top-level IDs missing or mismatched | Set `work_order_ids` array |
| 10 | Technical constraints | A WO lacks `## Technical Constraints`, or it omits a framework, storage, or named entity the PRD states | Copy the full constraint list into every WO |
| 11 | Repositories | With `input.project_repositories`, a WO lacks a `Repository:` line, names a repository not in the list, or WO dependencies form a cycle between repositories. In any project, a WO keeps the placeholder `owner/name` | Name one listed repository per WO, or leave the line out when there is no list; split or reorder WOs so dependencies between repositories point one way |
| 12 | Consistency | Within one WO, two statements cannot both hold: a requirement, constraint, implementation note or acceptance test asks for X while another asks for not-X (for example "sort the list newest first" and "keep the existing sort order unchanged"); or a WO contradicts a WO it depends on | Settle it from the PRD and FRDs and rewrite both statements to agree. If the PRD does not settle it, pick the reading that keeps the PRD's Must requirements, say which in the WO, and record the choice in `warnings` |
| 13 | Waves | A chain of work orders that could run in parallel: a dependency that is not needed to build or test, or tightly coupled pieces split into a chain of small WOs | Drop the dependency, or merge the coupled pieces (without breaking gate 6) |
| 14 | Complexity | A WO lacks a `Complexity: Low/Medium/High` line, or routine work is marked `High` | Add the line; keep `High` for hard-to-get-right work |
| 15 | Deferred work | A WO leaves part of its job for "later", "for now", "until X exists" or "once Y is ready" without a `Follow-up: WO-NNN` line, or the follow-up is missing from the plan or depends on nothing | Plan the deferred part as its own WO that depends on the WO producing what is missing, and name it as the follow-up |
| 16 | API contract first | An API WO adds or changes endpoints without updating the committed `openapi.json`; a web WO calls those endpoints without depending on that WO or without generating its types (`npm run api:pull && npm run api:generate`); any WO hand-writes API types | Make the updated spec part of the API WO's acceptance; make the web WO depend on it and generate its types from the spec |

Record unresolved items in `warnings`. Set `planner_complete: true` only when gates pass.

Completion: gates pass, `work_order_ids` populated, required artifact keys present.
