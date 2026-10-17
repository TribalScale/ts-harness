# Ops-Agent Heartbeat

The ops-agent heartbeat runs every 30 minutes. It uses `lightContext` and `isolatedSession`. It posts to the ops Slack channel only when there are deltas (changes since last tick). Reply `HEARTBEAT_OK` if nothing changed.

## Tasks

- **System health (every 30m):** Check service health endpoints, disk space on /vol/ts-harness-data (alert if >80% used), and Docker container status. Post a summary to ops channel if any check fails. Skip post if all green.
- **Disk check (every 2h):** If used disk > 90%, trigger an `approval-gate` escalation to the orchestrator.
- **Cert expiry (every 24h):** Check TLS cert expiry on the Tailscale HTTPS endpoint. Alert if < 14 days remaining.

## What NOT to Do
- Do not run terraform apply during a heartbeat — that is always an approval-gate action.
- Do not deploy or restart production services without orchestrator escalation.
