# Refinery Quality Gates (Governed Runtime)

Apply these checks to JSON markdown artifacts before returning.

| # | Gate | Fail condition | Fix |
|---|---|---|---|
| 1 | User-centered | AC describes internal mechanics | Rewrite from persona perspective |
| 2 | Testable | AC lacks trigger and observable behavior | Add concrete When/shall AC |
| 3 | Atomic | AC covers multiple behaviors | Split into separate ACs |
| 4 | No vagueness | Uses "appropriate", "properly", "etc." | Replace with specific behavior |
| 5 | Terminology | Conflicting terms across FRDs | Align in shared terminology |
| 6 | Implementation-agnostic | FRD prescribes APIs/DB/frameworks | Move to technical requirements |
| 7 | Role specificity | Generic "user" when persona applies | Use persona name |
| 8 | Outcome clarity | User story lacks "so that" | State user value |
| 9 | Dependency correctness | Circular/missing deps in feature tree | Fix dependency graph |
| 10 | PRD coverage | PRD section maps to no FRD | Add or update FRD |
| 11 | Artifact presence | No `frds_markdown` or `feature_tree_markdown` | Produce missing artifacts |
| 12 | Minimum content | Artifact under 200 chars | Expand with substantive content |

Record unresolved gate failures in `warnings`. Set `confidence` to `medium` or `low` if gates fail.

Completion: all gates pass and at least one required artifact key is present per `runtime-contract.md`.
