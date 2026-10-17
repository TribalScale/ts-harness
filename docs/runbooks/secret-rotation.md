# Secret Rotation Runbook

## When to rotate

- Scheduled rotation (quarterly recommended)
- Suspected compromise or breach
- Employee offboarding with access to secrets
- API key revocation by provider

---

## Procedure

### 1. Update the secret in AWS Secrets Manager

```bash
# Example: rotate Anthropic API key
aws secretsmanager update-secret \
  --secret-id ts-harness/anthropic-api-key \
  --secret-string "sk-new-key-value"
```

### 2. SSH to the instance and run the rotation script

```bash
sudo bash ~/enterprise-assets/scripts/ops/rotate-secrets.sh
```

The script:
1. Clears the phase-10 done-marker so secrets are re-fetched
2. Re-runs `10-fetch-secrets.sh` against AWS Secrets Manager
3. Updates `/vol/ts-harness-data/.ts-harness/.env` with refreshed runtime-readable keys
4. Refreshes `compose-env.sh` with the current DB and embedding-provider environment
5. Re-runs agent auth and channel initialization so `auth-profiles.json` and provider onboarding are refreshed
6. Updates the gateway token via `agent-runtime environment reload`
7. Recreates `agent-runtime`, `memory-api`, and `workflow-runner` when enabled

If the script cannot update the gateway token or local Postgres password, it exits non-zero with the manual operator step. Do not treat the rotation as complete until that step succeeds and the script can be re-run cleanly.

### 3. Verify the gateway started with the new key

```bash
docker compose logs --tail=30 agent-runtime | grep -iE "api|auth|key|error"
```

### 4. For Slack tokens — verify the Slack connection

```bash
# Check gateway Slack socket mode reconnected
docker compose logs --tail=30 agent-runtime | grep -iE "slack_socket|socket|connected"
```

### 5. For memory or embedding key rotation — verify memory-api

```bash
docker compose ps memory-api
docker compose logs --tail=30 memory-api | grep -iE "embed|memory|error"
```

---

## Rotatable secrets

| Secret | Stored at | Impact of rotation |
|--------|-----------|-------------------|
| `anthropic-api-key` | `ts-harness/anthropic-api-key` | Gateway restarts; in-flight LLM calls fail and retry automatically |
| `gateway-token` | `ts-harness/gateway-token` | Script updates the gateway auth token; existing UI sessions may need the new token |
| `slack-bot-token` | `ts-harness/slack-bot-token` | Slack channel briefly disconnects; socket mode reconnects automatically |
| `slack-app-token` | `ts-harness/slack-app-token` | Slack socket mode reconnects on gateway restart |
| `db-password` | `ts-harness/db-password` | Local backend: script attempts to align Postgres before recreating services; RDS backend: rotate the RDS secret first, then run the script |
| `openai-api-key`, `voyage-api-key`, `gemini-api-key`, `mistral-api-key` | `ts-harness/<provider>-api-key` | Agent auth profiles, gateway env, compose env, and `memory-api` are refreshed |
| `github-token` | `ts-harness/github-token` | Agent auth profiles are refreshed for `dev-agent` |
| `jira-token`, `jira-url`, `jira-username` | `ts-harness/jira-*` | Agent auth profiles are refreshed for `pm-agent` |
| `telegram-token`, `discord-token` | `ts-harness/<provider>-token` | Agent auth profiles are refreshed for `orchestrator` |

---

## Emergency: disable a secret immediately

If a key is compromised, deactivate it in the provider console **before** running the rotation script to prevent a window where the old key is still in use.

1. **Anthropic:** Revoke the key at console.anthropic.com > API Keys
2. **GitHub:** Delete the PAT at github.com > Settings > Developer settings > Personal access tokens
3. **Slack:** Revoke in api.slack.com > Your Apps > OAuth & Permissions
4. Create a new secret, update Secrets Manager, run the rotation script

---

## Memory API and sandbox worker tokens

Terraform generates `memory-api-token` and `sandbox-worker-token` on first apply.
`ignore_changes` keeps later applies from overwriting them. To rotate either token,
write a new value in Secrets Manager (or Secret Manager), then run `rotate-secrets.sh`.
That re-runs phase 10, refreshes the compose env, and recreates `agent-runtime`,
`memory-api`, `sandbox-worker`, and `workflow-runner` together. The fetch phase
refuses a blank or placeholder value, and both services fail closed without the
token. `GET /health` on the memory API does not require the bearer.

## Automating rotation (future)

AWS Secrets Manager supports automatic rotation via Lambda. For Anthropic/Slack tokens (which require manual key generation from external providers), this remains a manual procedure. Only `db-password` can be rotated fully automatically via RDS managed rotation.
