# Nightly smoke run: the web app against the real API

A web repository started from the `react-web` template runs its acceptance tests against a fake
API. The fake is checked against the backend's `openapi.json` (see
[testing-policy.md](testing-policy.md)), but a fake can only check what it is told: a web app
whose types drifted from what the real API sends, or a page that crashes on real data, passes
every scenario and breaks after sign-in. The nightly smoke run closes that gap. Each night, and
on demand, it builds the web app as users get it and runs it against the real API, started from
the API repository, on a real Postgres.

When it fails, the harness proposes one bug. A person approves it before anything is filed in
Jira.

## What runs

`.github/workflows/nightly-smoke.yml` in the web repository (template `react-web` 1.4.0 and
later), at 06:17 UTC and on demand:

1. Starts a Postgres service container.
2. Checks out the web repository and the API repository (`vars.API_REPO`, branch `dev` unless
   the run asks for another) with the read-only deploy key in `secrets.API_REPO_DEPLOY_KEY`.
3. In the API checkout: `npm ci`, then `npm run start:smoke` (template `node-api` 1.2.0 and
   later): applies migrations, seeds deterministic rows (`scripts/smoke-seed.ts`), and serves
   with the **CI-only test issuer** on. It waits for `/ready`.
4. In the web checkout: `npm run test:smoke`. Playwright builds the app with
   `vite build --mode smoke` (pointed at the API above), serves it, and runs only the features
   tagged `@smoke` (`features/smoke/`), with the fake API off:
   - **Sign in**: a token from the API's test issuer (`npm run token:test` in the API checkout)
     for the scenario's persona, with the same roles as the persona's Entra ID token.
   - **Every page**: opens each static route of `src/router.tsx` (the app lists them itself, so
     a new page is covered the day it is added; routes with parameters are skipped).
   - **One write**: adds a note.
   - Every call to the real API is checked: a 4xx/5xx or a call with no response, a request or
     response the backend's `openapi.json` does not allow, an uncaught page error, or the route
     error page each fail the scenario with the route.
5. On failure: uploads screenshots, traces, the Playwright report and the API log as the
   `nightly-smoke` artifact (14 days), then `scripts/smoke-report.mjs` posts a report to the
   harness.

The normal acceptance suite (`npm run test:bdd`) is unchanged: it excludes `@smoke` features.

### The CI-only test issuer

The API accepts tokens from the test issuer only when `AUTH_TEST_ISSUER=true` **and** `NODE_ENV`
is not `production`; with the flag in production it refuses to start. Its issuer
(`https://test-issuer.invalid/ts-harness`) and audience (`api://test-issuer`) can never match an
Entra ID token. It verifies ES256 signatures with the public key in `AUTH_TEST_PUBLIC_KEY`;
`start:smoke` generates a key pair per run (in the API checkout's `.smoke/`, git-ignored) unless
`AUTH_TEST_PRIVATE_KEY` and `AUTH_TEST_PUBLIC_KEY` are set. Nothing outside the run's runner holds
the private key.

## Secrets and variables (web repository)

| Name | Kind | What |
|---|---|---|
| `API_REPO` | Actions variable | The API repository, `owner/name`. Without it the job is skipped, not failed. |
| `API_REPO_DEPLOY_KEY` | Actions secret | Private half of an ed25519 key. The public half is a **read-only** deploy key on the API repository, titled `ts-harness nightly smoke: <web repo>`. |
| `HARNESS_SMOKE_URL` | Actions secret | `https://<harness public host>:8443/webhooks/smoke`. Optional: without it failures are not reported. |
| `HARNESS_SMOKE_TOKEN` | Actions secret | The web repository's report token. Optional, with the URL. |

### On the harness

| Name | Where | What |
|---|---|---|
| `SMOKE_REPORT_SECRET` | secret `smoke-report-secret`, created with a random value by Terraform (`infra/aws/secrets.tf`), which also lets the instance role read it; fetched by phase 10 | Every repository's report token is derived from it (HMAC of the repository name). Unset or unreadable: `/webhooks/smoke` answers 503 and phase 10 warns. |
| `SMOKE_REPORT_URL` | instance env | The public address of `/webhooks/smoke`, stored in each linked web repository. |

`scripts/phases/12b-github-webhook.sh` exposes `/webhooks/smoke` through Tailscale Funnel next
to `/webhooks/github` (port 8443) when `GITHUB_WEBHOOK_FUNNEL=true`. After setting either value,
re-render the runtime config and restart (`sync-runtime-config`, then phases 10 and 12; see
[redeploy.md](redeploy.md)).

## Automatic setup

When the harness links a web repository to its API (the bootstrap PR writes
`contract.openapi.repo` into the web repository's `.ts-harness.yml`), it also:

- keeps `.github/workflows/nightly-smoke.yml` in the bootstrap PR (a web repository with no API
  does not get it);
- sets the `API_REPO` variable;
- generates an ed25519 key pair, adds the public half to the API repository as a read-only
  deploy key, and stores the private half as `API_REPO_DEPLOY_KEY`;
- stores `HARNESS_SMOKE_URL` and `HARNESS_SMOKE_TOKEN` when `SMOKE_REPORT_URL` and
  `SMOKE_REPORT_SECRET` are set.

It runs when the bootstrap PR is opened or refreshed, and once per gateway process for a web
repository that already has the workflow. A deploy key and secret already in place are kept. The
ticket or queue summary lists what was done or what is missing. The harness never logs or
stores the private key or the token.

What it needs:

- **PyNaCl** in the gateway image. GitHub accepts Actions secrets only encrypted with the
  repository's public key (a libsodium sealed box), and the harness does not depend on a
  libsodium binding today. Without it only the `API_REPO` variable is set and the summary points
  here: do the rest by hand (below).
- **GitHub App permissions**: Secrets (write) and Variables (write) on the web repository;
  Administration (write) on the API repository, which deploy keys require. A missing permission
  is reported in the summary; nothing else fails.

## Manual setup

From a machine with `gh` signed in as someone who administers both repositories (`WEB=owner/web`,
`API=owner/api`):

```bash
dir=$(mktemp -d)
ssh-keygen -t ed25519 -N '' -C "ts-harness nightly smoke: $WEB" -f "$dir/key" -q
gh repo deploy-key add "$dir/key.pub" -R "$API" --title "ts-harness nightly smoke: $WEB"   # read-only by default
gh secret set API_REPO_DEPLOY_KEY -R "$WEB" < "$dir/key"
rm -rf "$dir"
gh variable set API_REPO -R "$WEB" --body "$API"
```

Failure reports (optional). On the harness host, the token goes straight into `gh secret set`
(the command refuses to print it to a terminal):

```bash
gh secret set HARNESS_SMOKE_URL -R "$WEB" --body "https://<harness public host>:8443/webhooks/smoke"
sudo docker exec docker-agent-runtime-1 python3 -m ts_harness.workflows.nightly_smoke token "$WEB" \
  | gh secret set HARNESS_SMOKE_TOKEN -R "$WEB"
```

An existing web repository started from an older template also needs the template's smoke files
(the workflow, `features/smoke/`, `features/support/smoke.ts`, the `test:smoke`/`build:smoke`/
`serve:smoke` scripts, `.env.smoke`), and its API needs `start:smoke`, `token:test` and the test
issuer from `node-api` 1.2.0. Copy them from `templates/` in this repository.

## Run it on demand

GitHub: the web repository's **Actions** tab, **Nightly smoke**, **Run workflow**; `api_ref`
picks the API branch, tag or commit (default `dev`). Or:

```bash
gh workflow run nightly-smoke.yml -R owner/web -f api_ref=feature/my-change
```

Locally, with Postgres running: in the API checkout `npm run start:smoke`, then in the web
checkout `SMOKE_API_URL=http://127.0.0.1:3000 SMOKE_API_DIR=../api npm run test:smoke`.

## How a failure becomes a proposal

`POST /webhooks/smoke` (`ts_harness.workflows.nightly_smoke`):

1. **Auth**: `Authorization: Bearer <token>` must be the report token for the report's `repo`; a
   token from one repository cannot report for another. 401 otherwise, 503 when
   `SMOKE_REPORT_SECRET` is unset.
2. **Validation**: `repo` and `api_repo` are `owner/name`, `run_url` and `artifact_url` are
   https, SHAs are hex, and `failures` is a non-empty list with a `scenario` each (at most 50,
   2,000 characters per error). 400 with the reason otherwise.
3. **De-duplication**: a failure is keyed by (web repository, scenario, route); the error text
   is left out, since it changes with the data. A key proposed in the last 24 hours, or whose
   program is still open, is known. A report with only known failures answers
   `{"status": "duplicate"}` and proposes nothing; a fix that later breaks again (its program
   ended more than a day ago) is proposed again. The keys are kept in `runtime_kv` (namespace
   `nightly-smoke`) for 30 days. A report naming its `api_repo` also records the new failures
   under `api:<owner>--<repo>`, so the post-merge guard can attribute them to the API repository
   (see [self-improvement.md](self-improvement.md)); that index plays no part in de-duplication.
4. **Proposal**: the new failures of one report go into **one** `bug_to_jira_work_orders`
   program started at `bug_intake`, with a bug report as its summary: each failing scenario
   with its step, route and error, the run URL, the artifact link and both commits. So a night
   where every page breaks is one proposal, not one per page. The program's own gates wait for
   a person (they show in the Inbox and on the program page): confirm the repository, review the
   diagnosis, approve the bug, then approve drafting and filing the work order. Nothing reaches
   Jira before that; cancel the program to drop the proposal.

Every report is audited as `smoke_report` with its outcome.

## Design frames every night

Each UI work order is compared with its frames before merge. What that can't see: a later
ticket changing a screen an earlier one built, the shell or a shared component drifting, a screen
that only looks right with the fake API's data. So the same nightly run checks every delivered
screen against its design (`ts_harness.workflows.design_conformance`).

1. **What to capture.** When QA passes a work order whose frame checklist gives a route, its
   frames are recorded for the web repository (`runtime_kv` namespace `design-frames`, newest 60
   per repository): key, route, size, the design image and the work order. The run fetches the
   list from `GET /webhooks/smoke/frames?repo=<owner/name>` with the report token.
2. **Capture.** `features/smoke/frames.feature` (react-web 1.5.0) signs in as an admin, opens each
   route at the frame's size against the real API, and saves `frames-out/<key>.png` plus a
   `manifest.json` noting routes it could not capture. A capture never fails the run.
3. **Send.** `scripts/frames-report.mjs` commits `frames-out/` to the branch
   `ts-harness-evidence/design-conformance` (one parentless commit, the work branch untouched;
   the workflow has `contents: write` for it) and posts `{repo, commit, run_url}` to
   `POST /webhooks/smoke/frames`.
4. **Compare.** The harness reads each capture from that commit and its design image from the
   intake bucket, and samples both: where both are a flat colour, colour pairs that differ at
   4 or more of the sampled points are findings, as are routes that could not be captured.
5. **Propose.** New findings (keyed by frame and colour pairs, known for 7 days in namespace
   `design-conformance`) go into one `bug_to_jira_work_orders` program per night, listing each
   frame with its Figma link, its capture and what differs. Its gates wait for a person, like a
   smoke failure. Answers: `proposed` (202), `duplicate`, or `conforms`.

A frame several tickets build in parts (`## Frame scope`) is judged per ticket only for the
part each owns, so this run is where the whole frame is compared. Its record lists every work
order that delivered part of it and the siblings that own the rest; the bug names them all
("built in parts by PROJ-11, PROJ-12, PROJ-13", and which are not delivered yet), so a gap
between tickets shows up as one.

Layout and missing panels need judgement, not sampling: the bug links both images so the
investigation, and the person approving it, can look. Funnel serves `/webhooks/smoke/frames`
through the `/webhooks/smoke` mount. Repositories created before react-web 1.5.0 need
`features/smoke/frames.feature`, the step in `features/steps/smoke.steps.ts`,
`scripts/frames-report.mjs` and the workflow changes copied from the template.

## Rotate the deploy key

Remove the old key and run the setup again, automatic or manual:

1. API repository, **Settings → Deploy keys**: delete `ts-harness nightly smoke: <web repo>`
   (or `gh repo deploy-key list -R owner/api` and `gh repo deploy-key delete <id> -R owner/api`).
2. Automatic: the next ticket or queue for the project, after a gateway restart, sees the key
   missing and creates a new pair and secret. Manual: the `ssh-keygen` block above.

To rotate every report token at once, change `SMOKE_REPORT_SECRET` and restart; the next setup
stores the new tokens (or set `HARNESS_SMOKE_TOKEN` by hand as above). Until then reports from
those repositories get 401.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Job skipped | `API_REPO` variable not set on the web repository. |
| "Check out the API" fails with a permission error | Deploy key missing or deleted, or the secret holds another key. Rotate it. |
| "Start the API" fails | The API does not start on a fresh database (a broken migration or seed), or it predates `start:smoke` (`node-api` 1.2.0). The API log is in the step output and the artifact. A report is still sent (`Smoke run setup`). |
| Report step says "HARNESS_SMOKE_URL / HARNESS_SMOKE_TOKEN are not set" | Reporting is optional; set them to get proposals. |
| Report answered 401 | The token was derived from another `SMOKE_REPORT_SECRET` (rotated), or set for another repository. |
| Report answered 503 | `SMOKE_REPORT_SECRET` is not set on the harness. |
| No new proposal for a failure that is still there | Expected: it is known (open program, or proposed in the last 24 hours). |
