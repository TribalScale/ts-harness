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

`runtime-cron-jobs.json` is mounted **read-write** so the console can persist jobs. Run **one** agent-runtime replica for cron file consistency (no multi-replica file sync).

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

- **Inbox** — skill drafts (`admin`/`owner`), tool approvals, workflow gates. Each row keeps an explicit type badge; a coloured status pill only appears when the backend reports a lifecycle status (for example `pending`, `blocked`, `approved`). Item type alone never drives success/warning colour.
- **Chat** — web channel (`web:` binding keys); Slack remains available. Tool and system transcript events render as annotations rather than assistant bubbles, and an empty transcript response never clears messages that have not been persisted yet.
- **Programs** — an always-visible detail header carries the program status pill, the waiting indicator, and the Advance/Cancel actions above the Overview, Tasks, and Artifacts tabs, so actions stay reachable from every tab. A failed advance or cancel keeps the Program selected and shows the error.
- **System** — Health, Cron, Skills, MCP, and Settings tabs; Health maps backend `ok`, `degraded`, and `unavailable` statuses directly and refreshes every 30 seconds. Skills (`#/system/skills`) is a read catalog plus attach handoff; see [skills-catalog.md](skills-catalog.md). MCP (`#/system/mcp`) overlays HTTP or catalog servers onto YAML bootstrap; see [mcp-settings.md](mcp-settings.md). Settings (`#/system/settings`) edits tenant Jira defaults, identity maps, and Slack routing overlays; see [system-settings.md](system-settings.md).

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
