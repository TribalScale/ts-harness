# Tenant isolation and role model review

Review date: 2026-11-22. Branch: `fix/tenant-isolation-review` (cut from `dev`).

## Scope

- Every HTTP route of the gateway (`runtime/ts_harness/gateway/asgi_app.py`), including the
  `/admin/*` BFF, `/webhooks/github`, `/channels/*`, `/scim/v2/*` and
  `/internal/coding-jobs/{job_id}/github-token`.
- Authentication and roles: `gateway/auth.py`, `gateway/access.py`, `policy/permissions.py`,
  `gateway/program_access.py`.
- The BFF modules the routes delegate to: `admin_bff`, `projects_bff`, `knowledge_bff`,
  `budgets_bff`, `github_bff`, `program_builder_bff`, `*_definitions_bff`.
- Chat ingress (`server.py` `handle_session_message`, `channel_confirmations.py`) and the
  agent's workflow tools (`tools/workflow_tools.py`).
- GitHub webhook and PR comment commands (`github_webhook.py`, `pr_commands.py`).
- Coding job tokens (`coding/github_tokens.py`, `scripts/memory_routes/coding_jobs.py`).
- The memory API (`scripts/memory-gateway.py`, `scripts/memory_routes/`).
- Secrets in responses and logs.

## Method

1. Listed every route from `asgi_app.routes` and read each handler, recording four things: does
   it authenticate, which permission it checks, is every lookup scoped to the caller's tenant
   (an id from another tenant must look like a missing one), and is program-level access
   checked when the resource belongs to a program.
2. Followed ids into the memory API to see whether the memory route scopes by tenant or trusts
   the id.
3. Traced chat replies and agent tool calls from a session to the workflow or program they act on.
4. Wrote a regression test for each fix. Added a contract test that inspects the route list and
   each handler's source (see [Keeping it true](#keeping-it-true)).

### Terms

- **Tenant**: `AuthContext.tenant_id`. In OIDC mode it comes from the token's tenant claim.
  With the service (static) token it comes from the request body. In single-tenant mode it is
  always `gateway.default_tenant_id`.
- **Permission**: `_require_admin(runtime, auth, "<permission>")`. The catalog is in
  `policy/permissions.py`. A service token holds `identity.service_permissions`, which is all
  permissions by default.
- **Program access**: `_require_program_level` / `_require_task_program_level`. These are
  enforced only with `TS_HARNESS_PROGRAM_ACCESS=enforce`. In the default `audit` mode a denial
  is only logged, but a program of another tenant is still refused with 404. Service callers
  skip program access.

## Route table

Legend:
- **T**: the lookup is scoped by `auth.tenant_id`, and another tenant's id returns 404.
- **P(level)**: program access at that level.
- **n/a**: the resource is deployment-wide.
- **Fixed**: the route changed in this branch.

### Probes, chat and dispatch

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| GET /healthz, GET /metrics | none | none | n/a | n/a | Open by design (probe, scrape) |
| GET /oauth/google/start | **Fixed**: was open, now bearer or session cookie | none (the person's own email only; a service caller may name any) | **Fixed**: was taken from the query, now `auth.tenant_id` | n/a | Fixed (F13) |
| GET /oauth/google/callback | **Fixed**: HMAC-signed state (was plain base64) | n/a | the tenant in the signed state | n/a | Fixed (F13) |
| GET /skills/doctor | bearer | none | **Fixed**: was taken from the `tenant_id` query parameter, now `auth.tenant_id` | n/a | Fixed (F8) |
| GET /mcp/tools, /connectors/google-workspace/tools, /memory/smoke | bearer | none | n/a (tool names only) | n/a | Accepted (D4) |
| POST /workflow/dispatch, /session/message | bearer | none (tool policy decides) | session is looked up within the tenant | per tool | Accepted (D4) |
| POST /workflow/cancel | bearer | none | audit row only, cancels nothing | n/a | OK (no-op) |
| POST /workflow/present-artifact | bearer | start_program | runtime | n/a | OK |
| POST /intake/uploads | bearer | none | stored under the tenant | n/a | Accepted (D4) |
| POST /cron/trigger | bearer | edit_config | n/a | n/a | OK |
| POST /sandbox/deny-check | bearer | none | n/a | n/a | OK (pure function) |

### Secrets, skills, approvals and auth

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| POST /admin/secrets/reload | bearer | edit_config | T | n/a | OK; returns the name only |
| GET /admin/skills, /admin/skills/{id}, /admin/skills/drafts[/{id}][/files/...] | bearer | view | n/a (shared catalog) | n/a | Accepted (D1) |
| POST /admin/skills/drafts, .../approve, .../reject | bearer | edit_config | n/a | n/a | Accepted (D1) |
| GET /admin/approvals | bearer | view | **Fixed**: listed every tenant's tool approvals, args included | n/a | Fixed (F1) |
| GET /admin/approvals/{id} | bearer | view | **Fixed**: T | n/a | Fixed (F1) |
| POST /admin/approvals/{id}/decide | bearer | approve_gate | **Fixed**: T, checked before deciding | n/a | Fixed (F1) |
| POST /admin/auth/session, /logout; GET /admin/auth/config | none (exchanges or reads the session) | none | n/a | n/a | Open by design |
| GET /admin/inbox | bearer | view | T for programs, sessions and gates; **Fixed** for tool approvals | P(read) list filter | Fixed (F1) |
| POST /admin/proposals/cancel | bearer | approve_gate | T (session) | n/a | OK |

### Programs

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| GET /admin/programs | bearer | view | T | list filtered when enforced | OK |
| GET /admin/programs/{id}, /status, /phases/{phase}/activity, /board | bearer | view | T (`_program_for_tenant`) | P(read) | OK |
| GET /admin/programs/{id}/evidence | bearer | edit_config | T | P(read) | OK |
| GET /admin/programs/{id}/tickets/{workflow_id}/story | bearer | view | T, and the workflow must belong to the program | P(read) | OK |
| GET /admin/live, /admin/history | bearer | view | T (programs listed by tenant) | P(read) per program | OK |
| POST /admin/programs/{id}/advance | bearer | the node's permission | T (mutation gate: 403 before 404) | P(write) | OK |
| PATCH /admin/programs/{id}; POST .../cancel, .../links; DELETE .../links/{to} | bearer | the program type's permission | T (links: both programs) | P(admin) | OK |
| POST /admin/programs/{id}/department-memory | bearer | promote_department | T, plus the department must match | P(write) | OK |
| GET /admin/programs/{id}/access | bearer | view | T | P(read) | OK |
| POST/DELETE .../access, PATCH .../visibility | bearer | view, plus program admin | T | P(admin) | OK |
| GET /admin/directory/search | bearer | view | T | n/a | OK |
| POST /admin/program-definitions/{type}/start | bearer | the entry node's permission | runtime | n/a | OK |
| POST /admin/workflow-definitions/{type}/start | bearer | start_program | runtime | n/a | OK |

### Workflows

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| GET /admin/workflows/{id} | bearer | view | T (task tenants) | **Fixed**: P(read) of the workflow's program | Fixed (F4) |
| GET /admin/workflows/gate-review | bearer | view | T (via the program) | P(read) | OK |
| POST /admin/workflows/approve, /reject | bearer | approve_gate | **Fixed**: the task's workflow must be in the caller's tenant | P(approve) | Fixed (F2) |
| POST /admin/workflows/decide | bearer | approve_gate | T (workflow), and the task is looked up inside it | P(approve) | OK |

### Deployment-wide configuration

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| Workflow, program and routing definitions; runtime and MCP settings; agents (GET) | bearer | view | n/a | n/a | Accepted (D1) |
| The same, writes (drafts, validate, activate, delete) | bearer | edit_config (MCP: edit_allowlists) | n/a | n/a | Accepted (D1) |
| GET /admin/workflow-fragments[/{id}], /drafts/{id} | bearer | view | n/a | n/a | Accepted (D1) |
| DELETE /admin/workflow-fragments/drafts/{id} | bearer | **Fixed**: was view, now edit_config | n/a | n/a | Fixed (F3) |
| POST/PUT fragment drafts, validate, activate | bearer | edit_config | n/a | n/a | OK |
| /admin/cron/jobs (GET: view; writes and trigger: edit_config) | bearer | as listed | n/a | n/a | Accepted (D1) |
| /admin/program-builder/* | bearer | edit_config | T (builder sessions) | n/a | OK |

### Sessions

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| GET /admin/sessions, /search, /{id}/messages | bearer | view | T | n/a | OK |

### Projects, notes, budgets and GitHub

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| GET /admin/github | bearer | view | n/a (installations are deployment-wide) | n/a | Accepted (D2) |
| POST /admin/github/sync | bearer | view, plus admin in the BFF | n/a | n/a | Accepted (D2) |
| GET /admin/budgets; POST /admin/budgets/{team}/raise | bearer | view; raise needs admin or team manager | T | n/a | OK |
| GET /admin/budgets/programs/{id} | bearer | view | T | **Fixed**: P(read) | Fixed (F9) |
| /admin/projects[/{id}] GET, POST, PUT | bearer | view; writes need admin or the owning team's manager | T (`projects_bff._project`) | n/a | OK |
| Project registries and repositories (CRUD, test) | bearer | admin or manager | T (project); the record must belong to the project | n/a | OK |
| /admin/projects/{id}/notes GET, POST | bearer | view; writes need admin or manager | T | n/a | OK |
| POST /admin/projects/{id}/notes/{note}/promote | bearer | manager of the project | **Fixed**: the note must belong to this project and tenant | n/a | Fixed (F5) |
| /admin/notes GET, POST; /{id} PATCH, /status, /restore | bearer | view; writes need admin (org) or manager (project) | T | n/a | OK |
| GET /admin/notes/{id}/versions | bearer | view | **Fixed**: was not checked for org notes | n/a | Fixed (F5) |
| GET /admin/knowledge/notes; GET /admin/knowledge/notes/{id} | bearer | view; proposals only for whoever could approve them (`can_see_proposal`) | T (search is tenant-filtered; a project note needs a project in the tenant) | n/a | OK |

### Webhooks, channels, SCIM and coding jobs

| Route | Auth | Permission | Tenant scoping | Program access | Verdict |
|---|---|---|---|---|---|
| POST /webhooks/github (signed) | HMAC-SHA256, constant time, delivery id required | n/a | always the default tenant | n/a | Accepted (D3) |
| POST /webhooks/github (no secret for the host) | bearer | **Fixed**: was none, now edit_config | auth | n/a | Fixed (F6) |
| POST /webhooks/smoke | per-repository report token (HMAC-SHA256 of `SMOKE_REPORT_SECRET` and the repository name), constant-time compare, checked before the body is validated; 503 without the secret | n/a: it can only start a `bug_to_jira_work_orders` program at `bug_intake`, whose gates wait for a person | always the default tenant | n/a | Accepted (nightly smoke, docs/runbooks/nightly-smoke.md) |
| PR comments `/retry` `/revise` `/accept` `/stop` | signed webhook, plus a GitHub login mapped to a person | approve_gate, plus P(approve) under OIDC | only the stuck tasks whose `pr_url` is this PR | P(approve) | OK |
| POST /channels/{channel}[/...] | platform signature (Slack, Telegram, Discord) or bearer | none (chat) | session within the tenant | channel checks | OK; D3 |
| POST /channels/{channel}/send-test | bearer | **Fixed**: was none, now edit_config | auth | n/a | Fixed (F7) |
| /scim/v2/* | `TS_HARNESS_SCIM_TOKEN` | n/a | the default tenant | n/a | Accepted (D3) |
| POST /internal/coding-jobs/{id}/github-token | job token (HMAC over the job id and expiry, checked by the memory API) | n/a | the job's own row; **Fixed**: the repo is checked against the workflow's tenant when the job is built | n/a | Fixed (F10) |

## Findings

Severity is the impact in a multi-tenant deployment (`single_tenant_mode: false`). A
single-tenant deployment is exposed only to the permission findings (F3, F6, F7).

### Fixed in this branch

| # | Severity | Finding | Fix |
|---|---|---|---|
| F1 | High | `/admin/approvals`, `/admin/approvals/{id}`, `/admin/approvals/{id}/decide` and the Inbox's tool approvals read and decided every tenant's tool approvals, including their tool arguments (email recipients, document contents). | The routes filter by `auth.tenant_id`, and another tenant's approval returns 404 before anything is decided. The Inbox calls `list_pending(tenant_id=...)`. |
| F2 | High | `/admin/workflows/approve` and `/reject` did not check the task's tenant. The program check is skipped for service callers, for `TS_HARNESS_PROGRAM_ACCESS=off`, and for tasks outside a program. The engine path (`approve_gate` / `revise_on_reject` hooks) calls `approve_task` / `reject_task` without a tenant, so the memory API did not scope it either. A caller in tenant B could approve tenant A's gate, run its hooks and start its next phase. | New `_require_task_in_tenant`: the task's workflow must belong to the caller's tenant, otherwise 404. This applies to service tokens too. |
| F3 | Medium | `DELETE /admin/workflow-fragments/drafts/{id}` needed only `view`, because the fragment helper picked the permission from the body mode and a DELETE has no body. | Only GET needs `view`. Every other method needs `edit_config`. |
| F4 | Medium | `GET /admin/workflows/{id}` checked the tenant but not program access, so a viewer could read the tasks of a private program's workflow by id. | It checks `read` on the workflow's program (resolved from its first task, walking up parent workflows). |
| F5 | High / Medium | Project note promote loaded the note by id only. A manager of any project could promote, and read, a note of another project or tenant, and the proposal was created in the note's tenant. Note versions returned another tenant's org note history. | Promote requires `scope == "project"`, the path's project and the caller's tenant. Versions checks the tenant. Both return 404 otherwise. |
| F6 | Medium | `/webhooks/github` falls back to bearer auth when no webhook secret applies to the request's host. The host comes from the caller's `X-GitHub-Enterprise-Host` header, so any signed-in console user could post unsigned `pull_request` merged or `installation` events. | The fallback requires `edit_config` (service token or admin). Signed deliveries are unchanged. |
| F7 | Medium | `POST /channels/{channel}/send-test` let any authenticated caller post as the bot to any channel. | Requires `edit_config`. |
| F8 | Low | `GET /skills/doctor` read the tenant from a query parameter. | Uses `auth.tenant_id`. |
| F9 | Low | `GET /admin/budgets/programs/{id}` checked the tenant but not program access. | Checks `read` program access. A denial returns `{"team": null}`, the same as no budget. |
| F10 | Medium | The coding job's repo check (allowlist, or a repo linked to a project) always used the default tenant. Another tenant's work order could target the default tenant's linked repos, and the job token then mints a GitHub token for that repo. | `build_coding_request(tenant_id=...)`: the stage passes the task's tenant and the tool passes the session's tenant. The directory GitHub login lookup uses the same tenant. |
| F11 | Medium | In OIDC multi-tenant mode, a token without a tenant claim took its tenant from the request body, so the caller could pick any tenant. | A token with no tenant claim is refused with 403 (`token carries no tenant claim`). Single-tenant mode is unchanged. |
| F13 | High | Google OAuth: `/oauth/google/start` was unauthenticated and took the tenant and email from the query string, and the `state` was unsigned base64. Anyone could connect *their own* Google account under another person's email in any tenant, so the harness would then send mail and edit documents for that person through the attacker's account. The callback also reflected the `error` query parameter into HTML unescaped (reflected XSS on the gateway origin). | The state is HMAC-SHA256 signed (`TS_HARNESS_OAUTH_STATE_SECRET`, else the OAuth client secret), and a forged or altered state is refused before the code exchange. Start requires a sign-in, uses `auth.tenant_id`, and lets a person connect only their own email (service callers may name any). The error page escapes its message. |
| F12 | Medium | Agent tools `workflow_status`, `cancel_workflow` and `resume_external_event` accepted a model-supplied `workflow_id` with no tenant check (prompt injection, or an id quoted from other content). | A `workflow_id` other than the session's own must belong to the session's tenant. Otherwise the tool returns `workflow not found`. |

### Accepted risk or design limitations

| # | Limitation | Why it is accepted / what would change it |
|---|---|---|
| D1 | Workflow, program and routing definitions, fragments, agents, runtime and MCP settings, skills and cron jobs are **deployment-wide**. An `edit_config` holder in any tenant changes them for every tenant, so in effect an admin is a platform admin. | The harness runs one configuration per deployment, so this is by design. A deployment shared by organizations that do not trust each other should run one deployment per organization, or add a platform-admin role distinct from tenant admin (follow-up). |
| D2 | GitHub App registrations and installations have no tenant. `GET /admin/github` shows every installation and its repositories to any viewer. A project manager in one tenant can register a repo covered by another tenant's installation. | The GitHub App is a deployment resource. Follow-up: a tenant column on installations, checked in `create_repository_admin` and at token minting. |
| D3 | Signed GitHub deliveries, Slack, Telegram and Discord events, and SCIM always act in the default tenant (there is no workspace/installation → tenant map). Waits in other tenants are never resumed by webhooks. | Correct for single-tenant deployments, which is how the harness is deployed today. Follow-up: map installation id and Slack team id to a tenant. Never read the tenant from the payload. |
| D4 | Chat (`/session/message`, `/workflow/dispatch`), `/intake/uploads` and the MCP tool listing need only a valid sign-in, not a permission. Even a role-less OIDC user can chat. | Chat actions go through tool policy, and starting, approving and deciding check permissions inside the chat flow. Restricting chat to `view` would be a product decision. |
| D5 | The memory API trusts its callers. It has one service token (`MEMORY_API_TOKEN`). Most routes take ids without a tenant (for example `GET /workflows/{id}`, `GET /projects/{id}`, `POST /knowledge-notes/{id}/promote`), and `approve`/`reject` scope only when a `tenant_id` is passed. Tenant isolation is enforced in the gateway, not in the memory API. | The memory API is internal (private network, service token). The coding job token is the only other credential, and it opens five actions on its own job. Follow-up: make `tenant_id` required on id routes so the database enforces isolation as defense in depth. |
| D6 | With `TS_HARNESS_PROGRAM_ACCESS=audit` (the default), program sharing is not enforced: any viewer in the tenant reads every program, and denials are only logged. | Rolling out sharing in audit mode first is deliberate. Set `enforce` once the logs show no false denials. |
| D7 | GitHub logins in `identity.github_logins` are deployment-wide, not per tenant, and a renamed login can be re-registered by someone else on GitHub. | Same as D3. The PR commands still require `approve_gate` and act only on that PR's own stuck tasks. |

### Follow-ups (not fixed here)

- Job GitHub tokens: the minted installation token is scoped to the job's repository but
  carries every App permission and is shared with the runtime's cache. Mint a fresh token per
  job with `permissions: {contents: write, pull_requests: write}`, and revoke it when the job
  finishes (`integrations/github_app.py`).
- Deployment waits match only `{sha, environment}`. A `deployment_status` from a fork with the
  same SHA could resume a release. Add `repo` to the wait's match (`workflows/release.py`).
- Webhook delivery dedupe checks first and remembers later, so two concurrent copies of a
  delivery can both run. Insert first, behind a unique constraint.
- `/webhooks/github` 500 responses and PR comment replies include `str(exc)` (GitHub API error
  bodies, never tokens). Return a generic message and log the detail.
- The coding job clones with the token in the URL (`implementation/repo.py`). Git redacts it in
  errors, but a credential helper or `http.extraHeader` would avoid relying on that.
- `projects_bff.update_registry_admin` writes the credential before confirming the registry
  belongs to the project. The write lands under the caller's own project path, so nothing
  crosses projects, but it can leave an orphaned secret.

## Secrets

No route returns a secret value:
- Registry credentials are write-only (`has_credential` only; `secret_arn` is stripped).
- The GitHub overview returns `webhook_secret: true/false`, never the secret or the key.
- `/admin/secrets/reload` returns the name only.
- Token-minting errors are truncated and never include the token.

No log line dumps request headers. Memory API errors log only the path.

## Keeping it true

`tests/runtime/test_tenant_isolation_routes.py` holds:

- A regression test per fix: a cross-tenant request gets 404, and a missing permission gets 403.
- `RouteAuthContractTests.test_every_route_authenticates_and_checks_a_permission`. It walks
  `asgi_app.routes` and fails when a handler neither authenticates (`_auth`, or a helper that
  calls it) nor checks a permission (`_require_admin`, or a helper that calls it). Exceptions
  are the explicit `_OPEN_ROUTES` (no auth, each with a reason) and `_AUTH_ONLY_ROUTES` (any
  signed-in caller). A new route lands in neither by accident: it fails the test until it
  checks a permission or is added to a list with a reason here.
- `test_program_routes_scope_the_program_by_tenant`: every `/admin/programs/{id}...` handler
  must load the program through `_program_for_tenant`, `_program_mutation_gate` or `_share_gate`.
- `test_channel_mount_authenticates`: channel routes use `_auth_channel`, and send-test checks
  a permission.

When adding a route that takes an id:
1. Load the record and compare its `tenant_id` with `auth.tenant_id`. Answer 404 on a mismatch,
   the same as missing.
2. Check program access when the record belongs to a program.
3. Check the permission for what the route does (`view` for reads, the specific action
   permission for writes), not just authentication.
4. Do not rely on the memory API to scope by tenant (D5).
