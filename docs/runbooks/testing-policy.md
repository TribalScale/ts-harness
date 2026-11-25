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

## API contract (web repositories)

A web repository started from the `react-web` template checks its fake API against the
backend's contract on every acceptance scenario (`features/support/contract.ts`): a path or
method the backend does not have, a request body it would reject, or a fake response it would
never send fails the scenario with the reason. The contract is `OPENAPI_CONTRACT` when set, else
the committed copy `src/api/openapi.json` (`npm run api:pull`).

The web repository names its backend in `.ts-harness.yml`, and the coding job fetches the
backend's current `openapi.json` from its default branch and sets `OPENAPI_CONTRACT`, so a
backend change the web repository has not caught up with fails its next change:

```yaml
contract:
  openapi:
    repo: org/api        # or a GitHub URL
    path: openapi.json   # default
    ref: dev             # default: the backend's default branch
    refresh_on_change: true  # default: a change to the spec starts a refresh run (below)
```

A project whose repositories are started from the templates gets this entry in the web
repository's bootstrap PR. An existing web repository adopts it with one ticket: copy
`features/support/contract.ts` and the reply check in `fixtures.ts` from the template, add
`ajv` and `ajv-formats`, commit the backend's `openapi.json` as `src/api/openapi.json`, and add
the `contract` entry.

### API types are generated, never written

The fake API is only as good as the types it is built from: a web app whose API types were
written by hand from planning documents, "until the backend's openapi.json is copied in", passes
every test against a fake built from the same wrong types and breaks against the real backend.
Four guards keep the types tied to the spec:

- **Planning.** The planner contract rejects a work order that defers part of its job ("until X
  exists", "for now", "later") without a `Follow-up: WO-NNN` work order that depends on the
  producer, an API work order that changes endpoints without updating the committed
  `openapi.json`, a web work order that calls those endpoints without depending on that work order
  or without generating its types (`npm run api:pull && npm run api:generate`), and any work order
  that hand-writes API types. Roles come from the project's repositories; the deferral and
  hand-written-types rules apply in every project.
- **Review and QA.** On a web PR, a file that declares API request or response types by hand is a
  `major` `correctness` code review finding, and a call to a path, method or field the spec does
  not have is a `blocker`. QA fails the PR for either.
- **CI** (`react-web` 1.3.0). `npm run check:api`, part of `npm run lint` and its own CI step,
  fails when `src/api/openapi.json` exists and a file under `src/api/` other than the generated
  `schema.d.ts` declares an interface or a type not taken from the schema, naming the files. The
  API client is typed by the generated `paths`, so a call to a path or method the spec does not
  have does not compile. In a TS Harness coding run, `npm run api:pull` reads the backend's spec
  from `OPENAPI_CONTRACT`. Repositories started from an older template add
  `scripts/check-api-types.mjs` and the `check:api` script from the template with one ticket.
- **Refresh on spec change.** When a PR merges in an API repository and changes its committed
  spec, the harness starts a refresh run on every web repository that shares a project with it
  and names it in `contract.openapi.repo` (see below).

### Refresh runs

The GitHub merge webhook (`github.merge`, subscriber `contract_refresh:on_merge`) checks the
merged PR's changed files. When the web repository's `contract.openapi.path` changed, the merge
went into the branch the web repository reads the spec from (`ref`, else the API's default
branch), and `refresh_on_change` is not `false`, it starts a `deliver_work_order` workflow on the
web repository with the work order `APIREFRESH-1 Refresh API types from org/api@<short sha>`: pull
the spec (`OPENAPI_SOURCE="$OPENAPI_CONTRACT" npm run api:pull && npm run api:generate`), fix
every type error against it, and pass lint, unit and acceptance tests. The PR then goes through
checks, code review, QA and the merge wait like any work order.

There is one refresh per web repository. A newer spec change cancels the refresh still running
(its coding job too) and starts one that updates the open refresh PR, found by its title, and
renames it to the new commit. The last run is kept in `runtime_kv` (namespace
`api-contract-refresh`, key `<owner>__<repo>`). Refresh runs need the webhook: the merge poller
only resumes waits and does not start them. Set `refresh_on_change: false` to turn them off for
a web repository.
