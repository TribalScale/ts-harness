# Post-deploy smoke test

Repeatable checklist after bootstrap phases 10–12 on a fresh instance with secrets populated.

## Installer flow (AWS or GCP)

Use the unified installer to select exactly one provider root, then populate secrets and smoke-test:

```bash
# Validate install contract + prerequisites
./scripts/install.sh validate --cloud aws --config config/install/aws.example.yaml
# or: python scripts/install.py validate --cloud gcp --config config/install/gcp.example.yaml

./scripts/install.sh plan --cloud aws --config path/to/install.yaml
./scripts/install.sh apply --cloud aws --config path/to/install.yaml
./scripts/install.sh status --config path/to/install.yaml
```

After `apply`, the CLI prints provider-specific secret population commands
(`aws secretsmanager put-secret-value` or `gcloud secrets versions add`).
Do **not** write operator secret values into Terraform state — placeholders are
seeded with `ignore_changes`. See `scripts/install_post_apply.py`.

Local install metadata lives under `.install/<environment>/install-metadata.json`.

## Prerequisites

- Secrets populated via the installer post-apply commands (AWS Secrets Manager or GCP Secret Manager), including Google Workspace SA JSON, credentials encryption key id, and gateway token.
- Migration **018** applied.
- Bootstrap phases **10–12** complete (`compose-up`, secrets wiring, automation cron).
- `TS_HARNESS_SKIP_GOOGLE_PROBE` unset in production (probe should pass with real delegation).
- Optional live smokes: `SMOKE_GMAIL_TO` set to a mailbox you control.

## Quick run

```bash
chmod +x scripts/ops/post-deploy-smoke.sh
./scripts/ops/post-deploy-smoke.sh
./scripts/ops/post-deploy-smoke.sh --google --workflow
```

## Manual checklist

| Step | Command | Expected |
|------|---------|----------|
| Health | `curl -s http://127.0.0.1:18789/healthz \| jq .status` | `"ok"`; components include `google_workspace` when configured |
| Metrics | `curl -s http://127.0.0.1:18789/metrics` | JSON counters including `agent_run_latency_ms_*` |
| Release gates | `RELEASE_GATES_PROFILE=post_deploy python3 scripts/release-gates.py` | exit 0 |
| Gmail send | `./scripts/ops/post-deploy-smoke.sh --google` | comms-agent delegation reaches succeeded/blocked-approval; verify sent mail when approved |
| Doc create | same `--google` run | pm-agent smoke returns succeeded/blocked with doc id in output |
| PRD workflow | `./scripts/ops/post-deploy-smoke.sh --workflow` | workflow record created; `draft_prd` task progresses |

## Tool approval verification (Slack)

1. Ask comms-agent to send a test email in a Slack thread.
2. Agent returns `approval_required` with preview.
3. Reply **yes send it** — tool executes once; session `pending_tool_approval` cleared.
4. Alternate path: `GET /admin/approvals` → `POST /admin/approvals/{id}/decide` with `approved=true` → retry with `approval_id`.

## OAuth connect (Slack actor email)

1. Ensure bot has `users:read.email` scope **or** map pilot users under `identity.slack_user_emails` in runtime yaml.
2. Run `google_workspace_connect` from a Slack thread; expect consent URL when `actor_email` resolves.

## Observability

- Cron publishes `GatewayHealthy`, `WorkflowRunnerHealthy`, `WorkflowTasksPending`, `WorkflowTasksFailed` via `scripts/ops/publish-runtime-metrics.sh`.
- CloudWatch alarms: gateway unhealthy (6 min), workflow runner unhealthy (6 min), pending tasks high (30 min).

## Notes

- Do **not** auto-run destructive Gmail sends on boot; use the smoke script explicitly.
- After deploy, see phase 12 marker comment in `scripts/phases/12-compose-up.sh` for the smoke script path.
