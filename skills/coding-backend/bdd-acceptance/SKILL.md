---
name: bdd-acceptance
description: Derive and automate BDD/Gherkin acceptance scenarios from work orders and planning docs. Use during WO implementation when Given/When/Then behavior can be inferred.
license: Apache-2.0
compatibility: opencode
metadata:
  audience: coding-backend
  owner: ts-harness
---

# BDD acceptance

Automate observable behavior as executable Gherkin scenarios. This is an outer acceptance layer; it does **not** replace unit/integration TDD from `coding-practices`.

## When to add or update scenarios

Derive scenarios when Given/When/Then (or equivalent acceptance criteria) can be inferred from **any** of:

1. The active work order under `docs/planner/work-orders/` (E2E, acceptance, user-visible outcomes)
2. Linked FRDs / requirements under `docs/refinery/`
3. Feature blueprints / contracts under `docs/foundry/`
4. Planner context that restates observable behavior for this WO

**Scope filter:** only behaviors inside this WO’s In Scope. Do not automate an entire FRD when the WO is a slice.

**Skip BDD** only for non-behavioral chores (pure refactor, dependency bump, renames with no observable behavior change).

If derivation is ambiguous, write the best scenarios you can from the docs, note assumptions in your summary, and continue — do **not** block waiting for Slack clarification.

## Style

- Business / ubiquitous language first; avoid HTTP status codes, table names, and framework internals unless needed for clarity.
- Exactly **one** When action per scenario.
- Use Background when two or more scenarios share the same preconditions.
- Prefer regular Scenarios; use Scenario Outlines only for clear data-driven rules.
- Minimal tags (`@wip` only when unfinished); do not over-tag.

**Good:**

```gherkin
Feature: Matchmaking lobby
  Scenario: Player joins an open lobby
    Given a lobby is open for new players
    When a player joins the lobby
    Then they appear in the lobby roster
```

**Bad (too technical):**

```gherkin
Scenario: Join lobby API
  Given users row with id 7
  When POST /api/lobbies/1/join returns 200
  Then JSON has status joined
```

## Stack detection

Use the target repo’s existing BDD stack if present:

- Cucumber / Behave / pytest-bdd / Playwright-BDD
- Existing `features/` (or project-equivalent) layout

Do **not** invent a second framework when one already exists. If none exists and scenarios are derivable, add features in the repo’s conventional location (prefer `features/`) and wire the lightest matching runner for the language already in use.

## Delivery

- Feature files and step definitions are in-repo artifacts.
- Do **not** commit, push, or open a PR — the host coding backend owns git/gh.

## Done when

- Derivable acceptance behaviors for this WO have Gherkin coverage (or an explicit skip reason for non-behavioral work).
- Scenarios stay within WO scope and use business language.
- Steps run (or are clearly marked `@wip` with reason) via the repo’s runner.
