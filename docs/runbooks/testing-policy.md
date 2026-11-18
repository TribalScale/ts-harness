# How delivered work is tested

Every ticket a coding job delivers is held to two kinds of tests:

- **Acceptance tests**: one per Given/When/Then criterion of the work order or ticket. Either
  Gherkin scenarios (BDD) or the repository's own end-to-end/integration tests, decided per
  repository as below.
- **Unit tests**: for every new or changed rule, including its error cases, whichever way the
  acceptance tests are written.

## BDD per repository (System → Projects → Testing)

| Setting | Gherkin when |
|---|---|
| **Auto** (default) | The repository has no tests yet, or already uses Gherkin (feature files, Cucumber, playwright-bdd, pytest-bdd, behave) |
| **Always** | Every repository of the project |
| **Never** | No repository; acceptance criteria use the repository's own runner |

Each program decides once per repository, the first time it sees the project's repositories,
and keeps the decision in its artifacts (`project_repositories[].bdd`, with `bdd_reason`). A
later change to the repository (the first ticket adding Jest, say) does not change it. Change
the setting before starting a program; running programs keep theirs.

When work orders are saved, each gets a `## Tests` section from its repository's decision:
`BDD: required` or `BDD: off`, with what that means. Jira tickets filed from them copy the
section; a ticket without one gets it when it is implemented (its program's decision, else the
project setting applied to the repository then). Verification and the coding agent follow the
line:

- `BDD: required`: the change must include a feature file named after the ticket key, run by
  the repository's BDD suite. A repository with no Gherkin runner gets one in the first ticket
  (the work order says so, and verification sends a fix pass until it exists).
- `BDD: off`: no feature files are required or added.
- No line (older work orders, tickets outside a project): decided from the repository as before.

## Unit tests

- Repositories started from a harness template enforce coverage in `npm test`: at least 80% of
  lines (75% of branches) of `src/modules/**/*.service.ts` (`node-api`) or `src/features` and
  `src/components` (`react-web`). A change that adds logic without tests fails the coding job's
  verification and CI.
- Code review blocks new business logic with no unit test exercising it, and reports logic
  whose error cases are untested (`governed-code-review` 1.5.0). QA fails a behavioural change
  whose unit tests failed (including a coverage threshold) or did not run (`governed-qa-gate`
  1.8.0).
- In large repositories, coding jobs run only the tests related to the change, with coverage
  turned off (a partial run cannot meet the thresholds); CI runs the full suite with coverage.

## CI

A repository with code but no CI on pull requests into its base branch is marked in planning
(`ci_on_pull_requests: false`). The planner then plans an "Add CI" work order first, which every
other work order for that repository depends on: a GitHub Actions workflow running what the
repository has, and `require_ci_checks: true` in `.ts-harness.yml`. Once it merges, the next
ticket or queue for the repository makes those jobs required checks on the base branch (as
`scripts/repo-governance.py --apply` does). Empty repositories get CI from their template
instead ([repo-templates.md](repo-templates.md)).
