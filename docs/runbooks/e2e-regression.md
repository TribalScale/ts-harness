# End-to-end regression and console browser tests

Unit tests cover each step; some failures only show on a real ticket going through the whole
pipeline. Two suites cover that gap:

- **Nightly end-to-end regression** (`scripts/ops/e2e-regression.py`): drives a fixed sample
  project through a running staging harness, from a PRD to merged PRs.
- **Console browser tests** (`admin-ui/e2e/`, Playwright): the built console against a mocked API.

## A. End-to-end regression on staging

### What it does

The script uses the gateway the way a person does:

1. Sends the checked-in PRD ([`tests/fixtures/e2e/sample_prd.md`](../../tests/fixtures/e2e/sample_prd.md),
   a two-feature "Bookshelf" reading list) as a chat message: `POST /session/message` with
   `channel: web`, the service token as the bearer and `actor` (`E2E_ACTOR`) in the body.
2. Answers **yes** to every chat proposal waiting on its own conversation (Inbox
   `pending_proposal` items with its `session_id`).
3. Approves every gate of its programs as it appears (`GET /admin/inbox`, `workflow_gate` items,
   `POST /admin/workflows/approve`).
4. Once the PRD phase has succeeded, says `file work orders in jira project <E2E_JIRA_PROJECT>`
   (override with `E2E_TRACK_MESSAGE`), approves the Refinery, Foundry and Planner gates, and
   waits for `file_jira` to file the tickets.
5. Says `implement all <E2E_JIRA_PROJECT>`, confirms and approves the queue.
6. Watches `GET /admin/live`; when a ticket of its programs reaches **Ready to merge**, merges the
   PR through the GitHub API with `E2E_GITHUB_TOKEN`, only when the PR is in `E2E_REPO`.
7. Finishes when the implement program has succeeded and nothing of its programs is in flight.

Programs that did not exist when the run started are the run's own: those created before
`implement all` are planning programs, those after are implement programs. Run it on a staging
instance nobody else is starting programs on at the same time.

The run fails at once on a stuck step (Inbox `stuck_step`), a program that ended `failed` or
`cancelled`, a ticket whose Live card is `stuck` or `failed`, a blocked chat message, a failed
merge, a PR outside the sandbox repository, no program 15 minutes after a message
(`E2E_START_GRACE_MINUTES`), or the global timeout (`E2E_TIMEOUT_MINUTES`, default 240).

### Assertions and report

At the end it reads `/admin/history`, each ticket's story (`/admin/programs/{id}/tickets/{wf}/story`
for `cost_usd`) and the programs' artifacts (`jira_story_keys`), and writes `report.json` and
`report.md` under `--out` (default `e2e-reports/<timestamp>`). Checks:

| Check | Meaning |
| --- | --- |
| planning completed / implementing completed / reached done | each stage finished |
| tickets filed | the planning program filed Jira keys, or tickets were built |
| PRs created / PRs merged | at least one PR, and every PR it saw merged |
| no stuck or failed steps | nothing in the failure list |
| no lost coding jobs | every ticket seen on Live has a `merged`, `accepted` or `done` outcome in History |

The report also records total time, time per stage, the tickets' coding cost, every PR and every
gate approved. The exit code is 0 only when every check passed (2 when it refused to start).

### Safety

The script refuses to run unless all of these hold:

- `E2E_SANDBOX=1`;
- `E2E_REPO` is listed in `E2E_SANDBOX_REPOS` and `E2E_JIRA_PROJECT` in
  `E2E_SANDBOX_JIRA_PROJECTS` (set both on purpose: the second list is the guard);
- every required setting is present.

The GitHub client merges and closes PRs only in `E2E_REPO`, re-checked on every call; a PR on any
other repository fails the run without being touched. Use a dedicated sandbox repository, a
sandbox Jira project, and a fine-grained GitHub token scoped to that one repository (Pull
requests: read and write, Contents: read and write). Tokens are never printed or written to the
report.

### Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `E2E_SANDBOX` | yes | must be `1` |
| `E2E_GATEWAY_URL` | yes | staging gateway, for example `https://staging.example.com` |
| `E2E_TOKEN` | yes | the gateway service token (`GATEWAY_TOKEN`) |
| `E2E_JIRA_PROJECT` | yes | sandbox Jira project key, for example `DEMO` |
| `E2E_REPO` | yes | sandbox repository `owner/name` |
| `E2E_ACTOR` | yes | the requester email; tickets are assigned to it, so map it in System → Settings → Identity |
| `E2E_GITHUB_TOKEN` | yes | token that can merge in the sandbox repository |
| `E2E_SANDBOX_REPOS` | yes | comma-separated sandbox repositories |
| `E2E_SANDBOX_JIRA_PROJECTS` | yes | comma-separated sandbox Jira projects |
| `E2E_PROJECT_KEY` | no | harness project key sent with chat messages (`project_key`) |
| `E2E_TRACK_MESSAGE` | no | planning-track message (default `file work orders in jira project <PROJECT>`) |
| `E2E_TIMEOUT_MINUTES` | no | global timeout, default 240 |
| `E2E_POLL_SECONDS` | no | poll interval, default 30 |
| `E2E_START_GRACE_MINUTES` | no | how long to wait for a program to appear, default 15 |
| `E2E_MERGE_METHOD` | no | `squash` (default), `merge` or `rebase` |
| `E2E_PRD_PATH` | no | another PRD file |

Staging prerequisites: the sandbox Jira project is in System Settings → Jira filing → Allowed
projects and linked to a project with the sandbox repository (System → Projects); the sandbox
repository is reachable by the GitHub App; `E2E_ACTOR` is mapped to a Jira account.

### Running it

```bash
# Keep the settings in a root-only file, never in the repository.
sudo install -m 600 /dev/null /etc/ts-harness/e2e.env   # then fill in the variables above
set -a; . /etc/ts-harness/e2e.env; set +a

python3 scripts/ops/e2e-regression.py --dry-run          # config, gateway, token and repo access only
python3 scripts/ops/e2e-regression.py --out /var/log/ts-harness/e2e/$(date +%F)
```

After the run it cleans up unless `--no-cleanup`: it cancels its programs that are still open
(`POST /admin/programs/{id}/cancel`, with the console's cancel compensation) and closes the PRs
it saw that are still open. To clean up by hand, for example after a killed run:

```bash
python3 scripts/ops/e2e-regression.py --cleanup-only --program <program-id> [--program <id> ...]
```

`--cleanup-only` closes **every** open PR in the sandbox repository.

### Nightly schedule (opt-in)

Nothing schedules it by default. To run it nightly, add a cron entry on a host that can reach the
staging gateway (the staging host itself or a CI runner), for example `/etc/cron.d/ts-harness-e2e`:

```cron
# m h dom mon dow user command
30 1 * * * ts-harness cd /opt/ts-harness && set -a && . /etc/ts-harness/e2e.env && set +a && python3 scripts/ops/e2e-regression.py --out /var/log/ts-harness/e2e/$(date +\%F) >> /var/log/ts-harness/e2e/cron.log 2>&1
```

Pull `dev` on that checkout before the run if staging tracks `dev`, so the fixture and script
match the deployed code. A non-zero exit leaves the report in the dated directory; wire the cron
log into your alerting, or run the same command from a scheduled CI job and fail the job on a
non-zero exit.

### Unit tests

`tests/test_e2e_regression_script.py` covers the decision function against fake API responses
(the whole happy path, stuck and failed steps, PRs outside the sandbox, timeouts), the safety
refusals, the report, and the loop against an in-memory fake API. No network.

## B. Console browser tests (Playwright)

`admin-ui/e2e/` runs the built console (`vite preview`) in headless Chromium with every API call
answered by Playwright route interception from fixture JSON in the real API shapes. No backend.

Covered:

- **Inbox**: the list; open a gate and approve; open a stuck step and revise with guidance;
  cancel a chat proposal.
- **Live**: a card with the current run's phase breakdown; **Details** opens the ticket story;
  findings **Show more**.
- **System Settings → Identity**: add a GitHub login mapping row and remove it (each change is saved through **Validate**, which writes the settings draft).

Run locally:

```bash
cd admin-ui
npm ci
npx playwright install chromium   # once
npm run test:e2e                  # builds, then runs playwright test headless
```

CI runs them in the `admin-ui` job after the build (`npx playwright install --with-deps chromium`,
then `npx playwright test`; `npm run typecheck:e2e` type-checks the specs). A failed run leaves `admin-ui/playwright-report/` and
`admin-ui/test-results/` (traces), both git-ignored.
