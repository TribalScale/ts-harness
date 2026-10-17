---
name: implement-work-order
description: Implement one governed work order from docs/planner specs. Use when asked to implement WO-NNN or when Primary spec points at docs/planner/work-orders/.
license: Apache-2.0
compatibility: opencode
metadata:
  audience: coding-backend
  owner: ts-harness
---

# Implement work order

You are implementing **one** work order in a host-managed git worktree.

## Source of truth

1. Read the primary work-order file under `docs/planner/work-orders/` (e.g. `WO-001-*.md`).
2. Use supporting context only as needed:
   - `docs/refinery/` (FRDs, requirements, feature tree)
   - `docs/foundry/` (components, feature blueprints, ADRs)
   - `docs/planner/context-graph.md` and `docs/planner/execution-phases.md`
   - `docs/prd/approved-prd.md` when present
3. Prefer the work order's In Scope / Out of Scope and E2E acceptance criteria over inventing requirements.

## Practice skills (required)

After reading the specs above, load and follow these skills from `.opencode/skills/` only:

1. **`coding-practices`** — TDD (RED-GREEN-REFACTOR), verification before claiming done, and root-cause debugging.
2. **`bdd-acceptance`** — derive/update Gherkin scenarios from the WO and/or `docs/refinery|foundry|planner` when Given/When/Then (or equivalent AC) is derivable, scoped to this WO’s In Scope.

Do not load other skill packs or planning workflows. Specs under `docs/` are already approved.

## Rules

- This is a **non-interactive host run**: implement immediately. Do not ask for approval, present a design for approval, or wait for a human.
- Implement **only** the assigned work order. Do not start sibling WOs.
- Prefer small, testable changes that satisfy acceptance criteria.
- Keep planning markdown under `docs/` in the change set when you touch related files; do not delete those specs.
- Follow existing project conventions, linters, and tests in the repo.
- Do **not** commit, push, or open a pull request — the host coding backend owns git/gh after you finish.
- Do **not** invent secrets, credentials, or external infrastructure outside the work order.
- Installing deps / fixing runners alone is **not** done — ship production code + tests for this WO.

## Done when

- Acceptance criteria for this WO are met (or clearly blocked with a short note in your summary).
- Unit/integration tests for changed behavior are green (TDD per `coding-practices`).
- BDD scenarios added/updated when derivable from the WO and/or planning docs (per `bdd-acceptance`), or an explicit skip for non-behavioral work.
- Diff stays within the work-order scope.
