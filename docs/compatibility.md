# TS Harness runtime compatibility matrix

This document is the release sign-off source of truth for the in-house TS Harness agent runtime.

## Canonical deployment

| Item | Value |
|------|--------|
| Project | `ts-harness` (repository root) |
| Cloud choice | Install-time `--cloud aws` **or** `--cloud gcp` (exactly one) |
| Terraform roots | `infra/aws` (AWS) or `infra/gcp` (GCP); never both for one install |
| Runtime service | `agent-runtime` (`docker/docker-compose.agent-runtime.yml`) |
| Runtime image tag | Terraform `agent_runtime_version` |
| Data mountpoint | `/vol/ts-harness-data` (both providers) |
| Workflow adapter URL | `AGENT_RUNTIME_URL` |

Provider adapters and secret backends differ; Compose overlays, ports (`18789` /
`18792`), health probes (`/healthz`, `/health`), and logical secret catalog keys
are shared. See [`runbooks/install.md`](runbooks/install.md).

## Required runtime endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /workflow/dispatch` | Workflow runner dispatch contract |
| `POST /session/message` | Normalized channel/session messages |
| `POST /cron/trigger` | Scheduler smoke and manual cron trigger |
| `GET /healthz` | Provider, memory-api, MCP, channels, slack_socket, sandbox, scheduler readiness |
| `POST /channels/{channel}` | Inbound HTTP Events API webhook (optional; public ingress) |
| `POST /webhooks/github` | GitHub `pull_request` merged → resume `github.merge` waits (`GITHUB_WEBHOOK_SECRET`) |
| `GET /mcp/tools` | MCP tool discovery gate |
| `GET /admin/skills/drafts` | List agent-created skill drafts (`admin`/`owner`) |
| `GET /admin/skills/drafts/{skill_id}` | Inspect draft body + scan diagnostics |
| `POST /admin/skills/drafts/{skill_id}/approve` | Promote draft to approved trusted skill + catalog reload |
| `POST /admin/skills/drafts/{skill_id}/reject` | Reject and remove draft |
| `GET /admin/auth/config` | OIDC vs static login discovery for admin-ui |
| `GET /admin/inbox` | Aggregated skill drafts, tool approvals, workflow gates |
| `GET /admin/programs` | List delivery programs (BFF → memory-api) |
| `GET /admin/programs/{id}/status` | Program phase status + waiting_on |
| `POST /admin/programs/{id}/advance` | Advance program phase (snapshot edge + materialize) |
| `POST /admin/programs/{id}/cancel` | Cancel program |
| `GET /admin/programs/{id}/evidence` | Signed program evidence JSON (`admin`/`owner`; SHA-256 in `X-Evidence-SHA256`; audited) |
| `GET /admin/program-definitions` | List built-in + managed program definitions |
| `GET /admin/program-definitions/catalog` | Workflow/adapter catalogs for Program Builder |
| `GET /admin/program-definitions/{program_type}` | Read active or built-in program definition |
| `POST /admin/program-definitions/drafts` | Create managed program draft |
| `GET/PUT/DELETE /admin/program-definitions/drafts/{id}` | Draft CRUD |
| `POST /admin/program-definitions/drafts/{id}/validate` | Semantic validation |
| `POST /admin/program-definitions/drafts/{id}/activate` | Activate immutable version + reload |
| `POST /admin/program-definitions/{program_type}/start` | Start a program from an active definition |
| `GET /admin/workflow-definitions` | List built-in + managed workflow definitions |
| `GET /admin/workflow-definitions/catalog` | Hook/agent/skill catalogs for Workflow Builder; `child_workflows` lists loadable child types with their `$workflow.*` input keys |
| `GET /admin/workflow-fragments` | List built-in + managed stage fragments (params, inputs, stage names) |
| `GET /admin/workflow-fragments/{fragment_id}?version=` | Read a fragment, optionally at a pinned version |
| `POST /admin/workflow-fragments/drafts` | Create managed fragment draft |
| `GET/PUT/DELETE /admin/workflow-fragments/drafts/{id}` | Fragment draft CRUD |
| `POST /admin/workflow-fragments/drafts/{id}/validate` | Fragment semantic validation |
| `POST /admin/workflow-fragments/drafts/{id}/activate` | Activate immutable fragment version (built-in ids are rejected) |
| `GET /admin/workflows/{id}` | Workflow + tasks for kanban |
| `POST /admin/workflows/approve` | Approve workflow gate task |
| `POST /admin/workflows/reject` | Reject workflow gate task |
| `GET/POST/PUT/DELETE /admin/cron/jobs` | Cron CRUD with scheduler hot-reload |
| `POST /admin/cron/jobs/{name}/trigger` | Force-run one cron job |
| `GET /admin/sessions` | List sessions (web chat) |
| `GET /admin/sessions/{id}/messages` | Transcript replay for chat UI |
| Admin UI (compose) | `docker/docker-compose.admin-ui.yml` on `${ADMIN_UI_PORT:-8080}` proxies API to agent-runtime |

## Memory backend contract

| Variable | `memory_backend = local` | Managed (`rds` / Cloud SQL) |
|----------|--------------------------|------------------------------|
| `TS_HARNESS_DB_HOST` / `TS_HARNESS_DB_HOST` | `ts-harness-db` compose service | Managed Postgres hostname (`MANAGED_DB_HOST`) |
| `TS_HARNESS_DB_PORT` / `TS_HARNESS_DB_PORT` | `5432` | Managed port (default `5432`) |
| `TS_HARNESS_DB_USER` / `TS_HARNESS_DB_USER` | `ts_harness` | Managed username |
| `TS_HARNESS_DB_NAME` / `TS_HARNESS_DB_NAME` | `ts_harness_memory` | Managed database name |
| `TS_HARNESS_DB_PASSWORD` / `TS_HARNESS_DB_PASSWORD` | Secret catalog `db-password` | Fetched via `MANAGED_DB_CREDENTIAL_REF` |

Session transcripts for production should use Postgres via `TS_HARNESS_SESSION_DB_URL`. Cloud bootstrap builds it from `TS_HARNESS_DB_*` (password URL-encoded, `sslmode=require` for managed databases) each time it runs `docker compose`, so a rotated DB password carries through; set `TS_HARNESS_SESSION_DB_URL` yourself to point sessions elsewhere. Local development may use `TS_HARNESS_SESSION_STORE` JSONL files. Channel chat message replay uses `TS_HARNESS_TRANSCRIPT_STORE` (defaults to `transcripts.jsonl` beside the session store) or the `runtime_transcript_messages` table when session Postgres is enabled.

To run Postgres session integration tests locally:

```bash
export TS_HARNESS_SESSION_DB_URL=postgresql://ts_harness:password@localhost:5432/ts_harness_memory
python3 -m pytest tests/runtime/test_postgres_sessions.py -q
```

## Release gate checklist

Run on the deployed host before promotion:

```bash
docker compose exec agent-runtime python3 /scripts/release-gates.py
```

Required gates (all must pass):

1. `runtime_health` — `/healthz` status `ok` with provider, memory-api, scheduler healthy
2. `memory_smoke` — `/memory/smoke`
3. `workflow_dispatch` — `/workflow/dispatch` returns `succeeded`
4. `slack_round_trip` — `/channels/slack/send-test` with `SLACK_RELEASE_GATE_CHANNEL`; when `SLACK_APP_TOKEN` is set, `/healthz` `slack_socket` must be `ok`
5. `mcp_discovery` — `/mcp/tools` returns registered `mcp-*` tools
6. `sandbox_denial` — destructive commands denied
7. `subagent_delegation` — orchestrator delegation smoke
8. `cron_heartbeat` — scheduler health + `/cron/trigger`
9. `tenant_isolation` — audit export scoped per tenant
10. `rbac_identity` — role checks enforced
11. `audit_export` — audit writer export works
12. `secrets_redaction` — secret values never leak in redaction helper
13. `budget_hard_stop` — tenant budget hard stop enforced
14. `delivery_program` — default `software_delivery` program definition loads and every pinned workflow reference resolves; program hooks registered
15. `channel_chat` — `/session/message` succeeds and persists a session
16. `workflow_record_create` — `POST /workflows/create` inserts a workflow row
17. `workflow_promotion_smoke` — memory-api healthy for governed delivery promotion
18. `workflow_hook_smoke` — in-process `WorkflowEngine.handle_reject` against prd_request skeleton
19. `prd_request_smoke` — promote + reject loop in-process
20. `continue_bridge_smoke` — bridge materializes downstream workflow tasks (`continue_delivery_smoke` alias)
21. `intent_routing_smoke` — yaml routing returns `governed_prd` for canonical phrase
22. `workflow_runner_import_smoke` — workflow engine importable in runner/runtime bundle
23. `skeleton_hooks_registered` — every hook referenced in workflow skeletons is registered

Gate list is driven by `config/release-gates.yaml`. Session metadata uses generic keys (`workflow_phase`, `approved_artifact`, `pending_bridge`, `bridge_linked`); legacy PRD-specific keys are read-only compat.

Apply migration `015_workflow_superseded.sql` for explicit `superseded` workflow status and `superseded_by_workflow_id`.

Hook engine memory-api endpoints: `PATCH /workflow/task/{id}/input`, `POST /workflow/resume-external`, `GET /workflows/{id}/children`, `POST /workflows/{id}/cancel`, `POST /workflows/{id}/complete`. Channel tools: `cancel_workflow`, `resume_external_event`. See [workflow-hooks.md](architecture/workflow-hooks.md).

## In-flight workflow skeleton versions

New channel threads use the federated delivery program model pinned from `config/programs/software_delivery.json` (or an activated managed program definition): `prd_request` → `jira_from_prd` / `work_orders_from_prd` → `implement_*`, linked by `delivery_programs.id` with a persisted definition snapshot and workflow-version pins. `deploy_release` remains in the workflow catalog but is not on this graph.

Pre-migration program rows with a null `program_definition_snapshot` continue through the explicitly named frozen payload `FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY` (`definition_version: legacy-compat`). That payload is not written back onto existing rows; historical backfill of `governed_delivery` chains writes `program_type`, definition pins, and the compatibility snapshot for newly synthesized programs only. The legacy `governed_delivery` skeleton remains loader-only for in-flight records; do not rename skeleton files for historical workflows.

Bootstrap phase `16-release-gates.sh` runs the same gate runner during instance bring-up.

## Slack inbound (Socket Mode)

Tailscale-only hosts use **Slack Socket Mode** for inbound messages (DMs,
@mentions, reactions). The runtime opens an outbound WebSocket using
`SLACK_APP_TOKEN`; no public Request URL is required.

| Variable | Purpose |
|----------|---------|
| `SLACK_BOT_TOKEN` | Outbound `chat.postMessage` and Bolt auth |
| `SLACK_APP_TOKEN` | Socket Mode WebSocket (`xapp-...`, `connections:write`) |
| `SLACK_SOCKET_MODE` | Enable listener when both tokens present (default `true`) |
| `SLACK_REQUIRE_MENTION` | Channel messages require @mention (default `true`) |
| `SLACK_REACTION_MODE` | `own` (default), `all`, or `off` |

HTTP Events API webhooks (`POST /channels/slack` + `SLACK_SIGNING_SECRET`) remain
supported for deployments with public ingress.

## Google Workspace connector

| Item | Value |
|------|--------|
| Connector id | `google-workspace` |
| Tool prefix | `google_workspace_*` |
| Auth | Service-account domain delegation (shared mailboxes) + per-user OAuth |
| Config | `connectors.google-workspace` in `ts-harness-runtime.yaml` |
| Discovery | `GET /connectors/google-workspace/tools` (auth required) |
| OAuth | `GET /oauth/google/start`, `GET /oauth/google/callback` |
| Health | `/healthz` component `google_workspace` |
| Secrets | `google-workspace-sa-json`, `google-oauth-*`, account emails in the cloud secret catalog |

Enable via `tenants[].enabled_connectors: [google-workspace]`. Agents bind mailboxes with `connector_bindings.google-workspace` (e.g. `comms`, `marketing`, `sales`, `pm`).

### Toolset splits

| Toolset | Tools | Typical agents |
|---------|-------|----------------|
| `google-workspace` | All registered connector tools | comms, sales (full allow lists) |
| `google-workspace-docs` | Drive search/get/create, Docs read/append | pm-agent, marketing-agent |

Declare `google-workspace-docs` under `toolsets:` in runtime config; agents add it to `toolsets:` without listing each Docs tool in `tools.allow`.

## Agent topology

The runtime orchestrates specialist workspaces including `marketing-agent`, `sales-agent`, `ops-agent`, and other bound agents declared in `config/ts-harness-runtime.yaml`. The orchestrator delegates to specialists via the in-house runtime; no upstream gateway process is required at runtime.

## Cron jobs

Release sign-off requires all 14 declared cron jobs, plus the `poll-github-merges` and `escalate-stale-waits` system jobs, to register and fire on schedule, including `stale-deal-alert`, `weekly-content-digest`, `weekly-pipeline-report`, and skill-bound `weekly-ops-health-check` (`skill_id: ops/health-check`). Cron jobs may set `skill_id` / `skill_hints` to inject governed skills (see [`runbooks/scheduled-skills.md`](runbooks/scheduled-skills.md)). Use `/cron/trigger` and scheduler health checks during gate runs.

## Tenant isolation tiers

| Tier | Description |
|------|-------------|
| Dedicated account | One cloud account/project per customer; runtime, memory DB, and secrets are fully isolated |
| Shared account, scoped rows | Single deployment with `tenant_id` on sessions, audit, memory scope, and MCP config |
| Separate schema | Shared RDS instance with per-tenant schema or database for memory and sessions |

Production release gates require `tenant_isolation` to pass before promotion.

## Cutover notes

- Runtime config is `config/ts-harness-runtime.yaml`.
- `enterprise_repo_url` must point at the TS Harness repository.
- Hardcoded `PHASE_ORDER` / `NEXT_PHASES` / `PHASE_SKELETON` / `ENTRY_REQUIREMENTS` / `PROGRAM_PHASES` are retired as live topology sources. Runtime, memory-api compatibility rendering, release gates, and historical backfill use program definitions or `FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY`.
- Live topology for new programs: `config/programs/software_delivery.json` or an activated managed definition under `/workflow-definitions/programs` (`TS_HARNESS_PROGRAM_DEFINITION_STORE`).
- Apply `022_program_definition_pinning.sql` (via `scripts/ops/migrate-db.sh`) so `delivery_programs` can store definition snapshots and resolved workflow versions. Pre-migration null-snapshot rows keep using `FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY` and are not mutated in place.
- Shared volume layout: workflow definitions at `/workflow-definitions/{drafts,active,history}`; program definitions nest at `/workflow-definitions/programs/{drafts,active,history}` on the same `ts-harness-workflow-definitions` volume.
- Activating a new program or workflow definition affects **new** program instances only; in-flight rows keep their pinned snapshot and workflow versions.
- Operator docs: [`runbooks/program-builder.md`](runbooks/program-builder.md), [`runbooks/governed-delivery-flow.md`](runbooks/governed-delivery-flow.md).

## Names

Configuration, environment variables, the console command, the webhook
signature header, and the runtime config file use TS Harness names only:
`TS_HARNESS_*`, `ts-harness`, `X-TS-Harness-Signature`, and
`ts-harness-runtime.yaml`. Database defaults, when none are set, are user
`ts_harness`, database `ts_harness_memory`, and compose service `ts-harness-db`.

Terraform state addresses use `harness` (`aws_instance.harness`,
`google_compute_instance.harness`). Cloud resource names, tags, the data disk,
and the secret prefix come from `project_name` and `secret_prefix`, which
default to `ts-harness`.
