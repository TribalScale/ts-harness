# Slack Multi-Agent Smoke Test

Use this after Terraform apply or after changing Slack, gateway auth, agent
routing, or specialist workspace configuration.

## 1. Confirm gateway is running

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}' | grep agent-runtime
sudo docker logs ts_harness-agent-runtime-1 --tail 100 2>&1 \
  | grep -iE 'failed|missing|SecretRefResolutionError|spawn_failed' || true
```

Expected: gateway container is running, and the log filter prints no new
startup/auth failures.

## 2. Confirm Slack Socket Mode is connected

TS Harness receives inbound Slack messages over **Socket Mode** (no public
Request URL required). Verify the listener is healthy:

```bash
curl -fsS http://127.0.0.1:18789/healthz | jq '.components.slack_socket'
sudo docker logs ts_harness-agent-runtime-1 --since 10m 2>&1 \
  | grep -iE 'slack_socket_connected|slack_socket_reconnect|slack_socket_auth' || true
```

Expected:

- `slack_socket.status` is `ok` when `SLACK_APP_TOKEN` is configured.
- Logs show `slack_socket_connected` after container start (no repeating auth errors).

If `slack_socket` is missing from health, Socket Mode is disabled (no app token
or `SLACK_SOCKET_MODE=false`). Outbound cron/send-test may still work.

### Slack app checklist (one-time)

In [api.slack.com](https://api.slack.com/apps) for your bot:

1. **Socket Mode** → Enable
2. **App-level token** with `connections:write` → stored as `slack-app-token` in Secrets Manager
3. **Event Subscriptions** → Enable (leave Request URL empty)
4. **Subscribe to bot events:** `app_mention`, `message.im`, `message.channels`, `message.groups`, `reaction_added`, `reaction_removed`
5. **OAuth scopes:** `app_mentions:read`, `chat:write`, `im:history`, `channels:history`, `groups:history`, `reactions:read`
6. Reinstall app to workspace; invite bot to the test channel

## 3. Confirm Slack and provider auth are available

Do not print raw secrets.

```bash
sudo bash -c '
for key in SLACK_BOT_TOKEN SLACK_APP_TOKEN ANTHROPIC_API_KEY OPENAI_API_KEY; do
  if grep -q "^${key}=" /vol/ts-harness-data/.ts-harness/.env; then
    echo "${key}=present"
  else
    echo "${key}=missing"
  fi
done
'
```

Expected: all four keys are `present` for two-way Slack.

## 4. Confirm pm-agent is configured and materialized

```bash
yq '.agents[] | select(.id=="pm-agent")' \
  /vol/ts-harness-data/.ts-harness/ts-harness-runtime.yaml

ls -la /vol/ts-harness-data/.ts-harness/workspace-pm-agent
ls -la /vol/ts-harness-data/.ts-harness/workspace-pm-agent/skills/memory-api
ls -la /vol/ts-harness-data/.ts-harness/agents/pm-agent/agent
```

Expected:

- `pm-agent` appears in `ts-harness-runtime.yaml`.
- Workspace contains `AGENTS.md`, `RULES.md`, `SOUL.md`, `HEARTBEAT.md`.
- `skills/memory-api` exists.
- `agent/auth-profiles.json` exists.

## 5. Confirm orchestrator subagent permissions

```bash
yq '.agents[] | select(.id=="orchestrator") | .subagents' \
  /vol/ts-harness-data/.ts-harness/ts-harness-runtime.yaml
```

Expected:

- `requireAgentId` is `true`.
- `allowAgents` includes `pm-agent`.

## 6. Send Slack test prompts

**DM:** open a direct message with the bot and send:

```text
Reply SLACK_DM_OK and summarize your role as orchestrator.
```

**Channel @mention** (bot must be invited to the channel):

```text
@YourBot Delegate to configured in-config agentId "pm-agent" using runtime "subagent", not ACP. Ask it to reply PM_AGENT_OK and summarize its role.
```

**Reaction (optional):** react with 👀 to a message the bot posted. With default
`SLACK_REACTION_MODE=own`, only reactions on bot messages are forwarded.

Expected:

- Bot replies in DM and in the @mention thread.
- `pm-agent` responds or the orchestrator returns a successful delegated result.
- No `spawn_failed`.
- No `Failed to spawn agent command: pm-agent`.
- No `Missing API key for provider "openai"`.

## 6b. Map Slack users before they start or approve work

A Slack reply is not an authorization by itself. The gateway resolves the
sender's email and looks it up in `identity.roles` (or `identity.group_roles`).

- Starting a workflow, program, advance, or bridge requires `start_program`
  (the `requester` bundle).
- Approving a pending tool requires `approve_gate` (the `approver` bundle).

An unmapped sender is refused with `missing permission ...`. Map the email
before the smoke test asks that person to reply yes.

## 7. Inspect failure logs if needed

```bash
sudo docker logs ts_harness-agent-runtime-1 --since 15m 2>&1 \
  | grep -iE 'pm-agent|spawn_failed|agent command|runtime|acp|subagent|SecretRefResolutionError|OPENAI_API_KEY|slack_socket'
```

Common failures:

| Symptom | Likely Cause |
| --- | --- |
| Outbound works but no reply to DMs/@mentions | Socket Mode not enabled in Slack app, missing `SLACK_APP_TOKEN`, or `slack_socket` health degraded |
| `slack_socket_disabled` in logs | Missing tokens, `SLACK_SOCKET_MODE=false`, or another process holds the socket lock |
| `Failed to spawn agent command: pm-agent` | Orchestrator used ACP instead of `runtime="subagent"` |
| `Missing API key for provider "openai"` | OpenAI not onboarded into gateway auth |
| `OPENAI_API_KEY is missing or empty` | Gateway `.env` lacks `OPENAI_API_KEY` |
| `Sandbox mode requires Docker` | Rendered config did not set sandbox mode to `off` |
