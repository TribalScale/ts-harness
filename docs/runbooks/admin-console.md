# Admin Console Runbook

Operator console for Inbox, Chat, Programs, and System. The browser talks **same-origin** to nginx (prod) or Vite (dev); APIs are proxied to agent-runtime.

## Cloud installs (AWS and GCP)

Bootstrap starts the console for you. Phase 12 adds `docker/docker-compose.admin-ui.yml` to the compose stack, and phase 6 points `tailscale serve` at the console on port 8080. Open `https://<tailscale-fqdn>` from a device on your tailnet. The cloud firewall only opens SSH, so port 8080 is not reachable from the internet.

Because the tailnet root now serves the console, nginx proxies every agent-runtime route, not only the console APIs. OAuth callbacks, SCIM, `/webhooks/github`, and `/channels/*` keep working on the tailnet URL.

On a host bootstrapped before this change, re-run phase 12 (its fingerprint changes, so it builds and starts `admin-ui`), then delete `/vol/ts-harness-data/.setup/phase-06-tailscale.done` and re-run phase 6 so the tailnet root moves from port 18789 to 8080. See [re-run-bootstrap-phase.md](re-run-bootstrap-phase.md).

## Production deploy (manual compose)

```bash
export ENTERPRISE_ASSETS_DIR=/path/to/ts_harness
export TS_HARNESS_CONFIG_DIR=/path/to/config-dir
export TS_HARNESS_TOOLS_ROOT_HOST=/path/to/tools
export ADMIN_UI_PORT=8080

docker compose \
  -f docker/docker-compose.agent-runtime.yml \
  -f docker/docker-compose.admin-ui.yml \
  up -d --build
```

Open `http://<host>:8080`. nginx serves `admin-ui/dist` and proxies `/admin`, `/healthz`, `/session`, `/cron`, `/workflow`, `/metrics`, and the remaining gateway routes (`/oauth`, `/scim`, `/webhooks`, `/channels`, `/skills`, `/mcp`, `/connectors`, `/memory`, `/sandbox`) to `agent-runtime:18789`. nginx re-resolves `agent-runtime` through Docker DNS, so recreating that container does not leave the console pointing at a stale IP.

The console session is an HttpOnly cookie named `ec_session`. `Secure` is set when `X-Forwarded-Proto` is `https`, or when `TS_HARNESS_SESSION_COOKIE_SECURE` is `true`. Leave it unset for the plain HTTP console on port 8080. A bearer pasted into curl still authenticates; the cookie origin check applies only when the cookie is the credential.

The redesigned console is a light-only operator UI. At 1024px and wider it uses a labeled left sidebar for Inbox, Chat, Programs, and System, and fixed 280px list panes. Below 1024px the sidebar collapses into a **Menu** button that opens a labeled navigation drawer, content spans the full viewport width, and detail views open as slide-overs. Closing a mobile detail returns to the list and keeps it closed.

### Artemis compose caveat

On Artemis, `memory-api` shares the `agent-runtime` network namespace. Any deployment that recreates `agent-runtime` must also recreate `memory-api`; otherwise the memory service can remain attached to the old runtime namespace and appear unavailable from `/healthz`.

### Production auth (OIDC)

1. Set runtime identity (see `config/ts-harness-runtime.yaml.tmpl` `identity:` block):

```yaml
identity:
  provider: oidc
  issuer: https://your-idp.example
  audience: ts-harness
  jwks_url: https://your-idp.example/.well-known/jwks.json
  client_id: <public-spa-client-id>
  authorization_endpoint: https://your-idp.example/authorize
  token_endpoint: https://your-idp.example/oauth/token
```

2. Env (container / host):

| Variable | Purpose |
|----------|---------|
| `OIDC_CLIENT_ID` | Optional override of `identity.client_id` |
| `OIDC_AUTHORIZATION_ENDPOINT` / `OIDC_TOKEN_ENDPOINT` | Optional endpoint overrides |
| `OIDC_SCOPES` | Default `openid profile email` |
| `TS_HARNESS_ALLOW_STATIC_TOKEN` | Set `false` in production |
| `TS_HARNESS_OIDC_STATIC_FALLBACK` | Keep `false` in production |
| `TS_HARNESS_REQUIRE_ADMIN_ORIGIN` | Set `true` so mutating `/admin` calls require `Origin` |
| `ADMIN_RATE_LIMIT_MAX` / `ADMIN_RATE_LIMIT_WINDOW_SECONDS` | Default 120 / 60s |

In OIDC mode the gateway still accepts `GATEWAY_TOKEN` from internal services (workflow runner, release gates, hooks) as `auth_provider: service`. It is never accepted when unset or left at the `dev-token` default, so set a strong `GATEWAY_TOKEN` and keep it out of browsers.

3. Put the console URL in `gateway.allowed_origins` (e.g. `https://claw.example.com` or `http://host:8080`).

### Cron CRUD note

With local state, `runtime-cron-jobs.json` is mounted **read-write** so the console can persist jobs, and all replicas on the host share the file.

With `TS_HARNESS_STATE_BACKEND=postgres` the cron file lives in Postgres (shared files, namespace `cron`) and each replica keeps a mirror of it ([shared-state.md](shared-state.md)). Edits take a lock in Postgres, so any replica can save; two saves at once give the second admin a conflict to reload. The scheduler lease holder picks up the change on its next tick. Phase 15 merges system jobs through the memory API, so admin-added jobs are kept.

## Local development

```bash
export GATEWAY_TOKEN=dev-token
ts-harness

cd admin-ui
# optional: VITE_GATEWAY_PROXY_TARGET=http://127.0.0.1:18789
npm install && npm run dev
```

Open `http://127.0.0.1:5173`. Static bearer login is the default when `identity.provider=static`.

## Smoke test

```bash
export GATEWAY_TOKEN=dev-token
export GATEWAY_URL=http://127.0.0.1:18789
export ADMIN_UI_ORIGIN=http://127.0.0.1:8080
chmod +x scripts/smoke/admin-console-smoke.sh
./scripts/smoke/admin-console-smoke.sh
```

Manual path: login → Inbox → Chat round-trip → Programs → System → Cron create/trigger.

## Tabs

- **Inbox** — skill drafts (`admin`/`owner`), tool approvals, workflow gates, stuck steps, knowledge notes and **chat proposals** (an offer such as "I can start the jira_to_pr program … reply yes" still waiting in a conversation; **Cancel proposal** drops it so a later yes there cannot start it, `POST /admin/proposals/cancel`; `/cancel` in the conversation does the same). Approving a gate is safe to repeat: the item leaves the list at once, and a second approval of the same gate returns `already_decided` instead of starting the next phase again. Each row keeps an explicit type badge; a coloured status pill only appears when the backend reports a lifecycle status (for example `pending`, `blocked`, `approved`). Item type alone never drives success/warning colour.
- **Live** (`#/live`, `GET /admin/live`) — every ticket or work order being built across the running programs you can read, refreshed every 10 seconds.
  - **Capacity:** coding jobs running (of `CODING_MAX_RUNNING`), queued, tickets in progress, and how many wait for you.
  - **One card per ticket:**
    - **current state:** the step, time in this step, total time, revisions (of 8, and whether the stronger model was used);
    - **who it waits on:** you, the agent, CI, or a coding slot;
    - **coding output:** the coding job's latest output line;
    - **links:** the PR, the Inbox decision and the program.
  - **Sorting:** waiting-for-you cards come first.
  - **Current coding run:** while a coding job runs, the card shows its model and its phases: Setup, Coding, Running tests, Pre-push review, Push. Finished phases have a tick and their time, and the current one shows its running time and what it is doing (for example "fixing test failures (pass 1)"). The worker reports the phase with each heartbeat (`coding_jobs.progress`, migration 050).
  - **Details:** each card (and each ticket on a program board) opens the ticket's story (`GET /admin/programs/<id>/tickets/<workflow_id>/story`), refreshed every 10 seconds, in your local time:
    - **where the time went:** coding, queued for a coding slot, CI, code review, QA and waiting for you, plus the coding cost;
    - **what happened, in order:** each coding run with its trigger (first run, code review findings, CI failure, guided, after triage, after `/revise` by a person), model, duration, cost, phases, the pre-push review result and the outcome (PR opened or updated, lost, failed); CI results; each code review or QA verdict with its blocking findings (severity, file and line, message; the first two shown, the rest on "Show more"); each wait for you with its length, and who decided with their guidance; merge and Jira close.
    - Revise decisions made in the Inbox record the person's email, so the story names them.
  - **History** tab (`#/live/history`, `GET /admin/history?days=&project=&repo=&outcome=&q=`): finished tickets and work orders across programs, newest first.
    - **Each run:** its outcome (merged, accepted, stopped, cancelled, failed, replaced by a later run), who decided (the merger, the person or the harness that accepted or stopped it), when it finished, total time, revisions, PR and program links, and its timeline.
    - **Filters:** by period (7 to 90 days), project, repository and outcome; search by ticket key, title, program or repository.
  - **Metrics** tab (`#/live/metrics`, `GET /admin/metrics?days=&project=&repo=&running=`): delivery quality over the tickets and work orders that finished in the period (default 14 days; `running=1` adds the ones still running), across the programs you can read (the same access check as History), to see whether a change to the harness helps.
    - **First-try pass:** the share of finished runs (replaced runs left out) that merged with zero revisions of their coding step.
    - **Revisions per ticket:** median, mean and max, split by what triggered each revision (code review, QA, CI, guided, triage, `/revise` by a person, re-run after a failed run), using the same trigger rules as the ticket story.
    - **Time to merge:** first coding start to merge, over merged tickets; and the share of time waiting for a person against agent time (coding, code review and QA), from the story totals.
    - **Pre-push review catch rate:** the share of coding runs with a pre-push review (not `no_review`) whose review fixed at least one blocking issue, and how many it fixed.
    - **Gate disagreement:** of the successful coding runs whose pre-push review passed (its last round found nothing blocking) and that reached a code review verdict, the share the code review gate then blocked.
    - **Cost per ticket:** the sum of the coding jobs' usage `cost_usd`, median and total, and the share spent on the stronger model (runs whose coding step was explicitly sent to a model, a revision or guided attempt, and any run on that same model).
    - **Lost coding jobs:** how many coding jobs were lost (no heartbeat), of all coding runs.
    - **By week:** first-try pass rate and mean revisions per ticket for each week a ticket finished in, when the period spans more than one week.
    - **Tickets:** costliest first (then most revisions), with outcome, revisions by trigger, cost, time to merge and time waiting for you; **Story** opens the ticket's story.
    - Each ticket is read once (its workflow and one coding-jobs query), so a long period over many programs takes a few seconds.
- **Chat** — web channel (`web:` binding keys); Slack remains available. Tool and system transcript events render as annotations rather than assistant bubbles, and an empty transcript response never clears messages that have not been persisted yet.
- **Programs** — an always-visible detail header carries the program status pill, the waiting indicator, and the Advance/Cancel actions above the Overview, Tasks, and Artifacts tabs, so actions stay reachable from every tab. A failed advance or cancel keeps the Program selected and shows the error. A ticket program's Overview also shows its **board** (`GET /admin/programs/{id}/board`): columns for waiting on blockers, ready, coding, in review, waiting for you, done (and stopped), each ticket's open Jira blockers as chips, the ticket's timeline when opened (with **All attempts** listing earlier runs of the same ticket, their outcome and timeline), and "where the time went" across coding, CI, code review, QA, waiting for merge and waiting for a decision.
- **Memory** (`#/memory`, `#/memory/<note id>`; `GET /admin/knowledge/notes?q=&scope=&project_id=&status=&kind=`, `GET /admin/knowledge/notes/<id>`) — every knowledge note you can read, org and project, in one list. There is no graph view.
  - **Search:** the box uses hybrid search (full-text and embeddings, fused with reciprocal rank fusion). When embeddings are unavailable the page says results are full-text only. With no search text the list is newest first.
  - **Filters:** scope (org or project), project, status (proposed, active, retired, rejected) and kind (avoid, howto).
  - **Each row:** title, the start of the body, kind, scope and project, status, a **Drafted from a run** tag for notes the failure drafter wrote, how many runs used it and when it changed.
  - **Detail:** the full text, why, applies_to, evidence (with **Drafted from** the run, PR and tasks it came from), who proposed and approved it, and its version history.
  - **Actions** follow the rules in [knowledge-notes.md](knowledge-notes.md): approve or reject a proposal, edit or archive (retire) an active note. Org notes and exceptions need an admin; project notes an admin or a manager of the owning team. Everyone with the console's view permission reads active and retired notes; a proposal is listed only for people who could approve it (as in the Inbox). Edits are compare-and-set; a 409 reloads the note.
- **System** — Health, Cron, Skills, MCP, and Settings tabs; Health maps backend `ok`, `degraded`, and `unavailable` statuses directly and refreshes every 30 seconds. Skills (`#/system/skills`) is a read catalog plus attach handoff; see [skills-catalog.md](skills-catalog.md). MCP (`#/system/mcp`) overlays HTTP or catalog servers onto YAML bootstrap; see [mcp-settings.md](mcp-settings.md). Settings (`#/system/settings`) edits tenant Jira defaults, identity maps, and Slack routing overlays; see [system-settings.md](system-settings.md).
  - **Projects** (`#/system/projects/<id>`): each project's page starts with a **Setup checklist** (`GET /admin/projects/<id>/checklist`, read access like the project itself) that checks, live, what a new project needs. Each item is **Ready**, **Warning** or **Failing** with a one-line explanation and, when not ready, how to fix it and an **Open** link to the console page where it is fixed. The header shows "N of 8 ready"; **Re-check** runs it again. The items:
    1. **Jira project allowed:** the project has a Jira project key and it is in System Settings → Jira defaults → Allowed projects (`program_defaults.jira_filing.allowed_projects`; no allowlist allows every project).
    2. **Jira reachable:** the Jira service account can read the project (`GET /rest/api/3/project/<KEY>`).
    3. **Repositories linked and reachable:** at least one repository is linked; for each, the GitHub App is installed and can read it, and its base branch exists. A repository the App is not installed on but the fallback token reads is a warning.
    4. **Repositories allowed:** each repository is inside the tenant GitHub org when one is set (linked repositories already pass the repo allowlist).
    5. **Branch protection requires harness checks:** each base branch is protected and requires `harness/code-review` and `harness/security-scan`, plus `harness/independent-review` while independent review is on (the same check the delivery flow warns with; see [github-branch-protection.md](github-branch-protection.md)).
    6. **CI on pull requests:** a GitHub Actions workflow runs on pull requests into each base branch (a warning: without it planning adds an "add CI" work order first).
    7. **Approvers mapped to GitHub and Jira:** System Settings → Identity has GitHub logins, including one for you (the person viewing); a missing Jira account mapping is a warning when the project has a Jira key.
    8. **Project settings:** revisions (`revision_autonomy`), parallel delivery (`max_parallel_tickets`, 1 to 8) and BDD mode (`bdd_mode`) hold valid values.

    Checks run side by side with a 10 second limit in all; a check that errors or runs out of time shows "Could not check: …" as a warning and the others still report.

### Accessibility notes

- Tabs expose stable ids with `aria-controls`, matching `role="tabpanel"` regions use `aria-labelledby`, and ArrowLeft/ArrowRight move both selection and DOM focus.
- Focus rings come from the Astryx neutral theme, and every button, link, and field still shows a visible `:focus-visible` outline on light surfaces.
- Long list rows still rely on the native `title` attribute for their truncated full text. Native tooltips are not keyboard- or touch-accessible and cannot be styled; the full value is always reachable through the detail pane, so this is accepted rather than replaced with a custom tooltip component.

## Session transcript tenancy

`GET /admin/sessions/{id}/messages` is scoped to the authenticated `tenant_id` in both the ASGI and stdlib gateway paths. A session belonging to another tenant returns `404 not found` — the same response as an unknown session — so the endpoint cannot be used to probe for session existence across tenants.

## Chat contract

The web console posts authenticated Chat messages to `POST /session/message` with:

```json
{
  "channel": "web",
  "source": "web",
  "text": "Report system status",
  "thread_id": "web:<binding>",
  "binding_key": "web:<binding>"
}
```

The response must include `session_id` and either displayable `output.summary` content or an explicit `error`. The persisted transcript is read from `GET /admin/sessions/{session_id}/messages`, where the user turn is expected as `content` or `text`. The smoke script validates this round trip without printing the bearer token or full model response.

## Local verification

```bash
cd admin-ui
npm test
npm run lint
npm run build
cd ..
PYTHONPATH=runtime .venv311/bin/pytest \
  tests/runtime/test_contract.py \
  tests/runtime/test_admin_inbox_bff.py \
  tests/runtime/test_cron_store.py \
  tests/runtime/test_auth_origin.py \
  tests/runtime/test_rate_limit.py -q
```

Smoke against a running gateway:

```bash
export GATEWAY_TOKEN=dev-token
export GATEWAY_URL=http://127.0.0.1:18789
export ADMIN_UI_ORIGIN=http://127.0.0.1:8080
./scripts/smoke/admin-console-smoke.sh
```
