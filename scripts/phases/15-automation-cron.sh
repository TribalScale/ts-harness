#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="15-automation-cron"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
COMPOSE_ENV="$DATA_DIR/.setup/compose-env.sh"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

HEALTH_CRON_PATH="PATH=/usr/local/bin:/usr/bin:/bin"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

source_required_env_files "$SECRETS_ENV" "$COMPOSE_ENV"
reconstruct_compose_files
if ! require_slack_channel_env "$PHASE_NAME"; then
    exit 1
fi

TZ_VAL="${AGENT_TIMEZONE:-UTC}"
STANDUP_CHAN="${SLACK_STANDUP_CHANNEL:-}"
OPS_CHAN="${SLACK_OPS_CHANNEL:-}"
DEV_CHAN="${SLACK_DEV_CHANNEL:-}"
TEAM_CHAN="${SLACK_TEAM_CHANNEL:-}"
MARKETING_CHAN="${SLACK_MARKETING_CHANNEL:-}"
SALES_CHAN="${SLACK_SALES_CHANNEL:-}"

PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$0" "$SECRETS_ENV" "$COMPOSE_ENV")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

CRON_CONFIG="$ENTERPRISE_ASSETS_DIR/config/runtime-cron-jobs.json"
# Docker bind-mount creates a directory if the host path is missing at first up.
if [[ -d "$CRON_CONFIG" ]]; then
    echo "[phase:${PHASE_NAME}] Removing directory at $CRON_CONFIG (Docker bind-mount placeholder)." >&2
    rm -rf "$CRON_CONFIG"
fi
mkdir -p "$(dirname "$CRON_CONFIG")"
if [[ ! -f "$CRON_CONFIG" ]]; then
    echo '{"jobs":[]}' > "$CRON_CONFIG"
    chown "${ADMIN_USER}:${ADMIN_USER}" "$CRON_CONFIG"
fi

SYSTEM_JOBS="$(mktemp)"
trap 'rm -f "$SYSTEM_JOBS"' EXIT

echo "[phase:${PHASE_NAME}] Building system scheduler jobs (timezone: $TZ_VAL) ..."
python3 - "$SYSTEM_JOBS" "$TZ_VAL" "$STANDUP_CHAN" "$OPS_CHAN" "$DEV_CHAN" "$TEAM_CHAN" "$MARKETING_CHAN" "$SALES_CHAN" <<'PY'
import json
import sys
from pathlib import Path

out, tz, standup, ops, dev, team, marketing, sales = sys.argv[1:]

def job(name, cron, agent, message, channel=None, timeout=300):
    payload = {
        "name": name,
        "cron": cron,
        "timezone": tz,
        "agent_id": agent,
        "prompt": message,
        "timeout_seconds": timeout,
    }
    if channel:
        payload["announce"] = {"channel": "slack", "to": f"channel:{channel}"}
    return payload

jobs = [
    job("daily-standup", "0 9 * * *", "comms-agent", "Execute daily-standup per standing orders.", standup),
    job("ci-pipeline-health", "5 9 * * *", "ops-agent", "Execute ci-pipeline-health per standing orders.", ops),
    job("overnight-alerts", "10 9 * * *", "ops-agent", "Execute overnight-alerts per standing orders.", ops),
    job("stale-pr-check", "15 9 * * *", "dev-agent", "Execute stale-pr-check per standing orders.", dev),
    job("sprint-progress-report", "0 8 * * 1", "analyst-agent", "Execute sprint-progress-report per standing orders.", team, 600),
    job("weekly-merged-prs", "15 8 * * 1", "dev-agent", "Execute weekly-merged-prs per standing orders.", dev),
    job("stale-ticket-alert", "30 8 * * 1", "pm-agent", "Execute stale-ticket-alert per standing orders.", team),
    job("cost-anomaly-check", "45 8 * * 1", "analyst-agent", "Execute cost-anomaly-check per standing orders.", ops),
    job("weekly-content-digest", "0 8 * * 1", "marketing-agent", "Execute weekly-content-digest per standing orders.", marketing),
    job("weekly-pipeline-report", "0 8 * * 1", "sales-agent", "Execute weekly-pipeline-report per standing orders.", sales),
    job("stale-deal-alert", "30 9 * * *", "sales-agent", "Execute stale-deal-alert per standing orders.", sales, 180),
    job("velocity-report", "0 9 1 * *", "analyst-agent", "Execute velocity-report per standing orders.", team, 600),
    job("memory-maintenance", "15 9 1 * *", "orchestrator", "Execute memory-maintenance: expire old memories, flag low-confidence rows, archive stale entries.", None, 600),
    job("skills-audit", "30 9 1 * *", "orchestrator", "Execute skills-audit: review agent capabilities and scheduled routines for improvement opportunities."),
    # Deterministic outbound poller (no LLM). Resumes await_human_merge without public webhooks.
    {
        "name": "poll-github-merges",
        "kind": "system",
        "handler": "poll_github_merges",
        "interval_seconds": 60,
        "active_start": "00:00",
        "active_end": "23:59",
        "timezone": "UTC",
    },
    # Escalates waits and gates past their deadlines; expired waits stay blocked with a recommended action.
    {
        "name": "escalate-stale-waits",
        "kind": "system",
        "handler": "escalate_stale_waits",
        "interval_seconds": 900,
        "active_start": "00:00",
        "active_end": "23:59",
        "timezone": "UTC",
    },
    # Pulls people and teams from identity.directory and disables people who left.
    {
        "name": "sync-directory",
        "kind": "system",
        "handler": "sync_directory",
        "interval_seconds": 900,
        "active_start": "00:00",
        "active_end": "23:59",
        "timezone": "UTC",
    },
    # Marks coding jobs lost when their worker stops heartbeating or overruns; the task resumes blocked.
    {
        "name": "sweep-coding-jobs",
        "kind": "system",
        "handler": "sweep_coding_jobs",
        "interval_seconds": 60,
        "active_start": "00:00",
        "active_end": "23:59",
        "timezone": "UTC",
    },
    # Syncs the budgets config and warns when a team nears or reaches its monthly LLM budget.
    {
        "name": "budget-alerts",
        "kind": "system",
        "handler": "budget_alerts",
        "interval_seconds": 300,
        "active_start": "00:00",
        "active_end": "23:59",
        "timezone": "UTC",
    },
    # Records each GitHub App installation's remaining REST quota for the dashboard and alarm.
    {
        "name": "github-rate-limits",
        "kind": "system",
        "handler": "github_rate_limits",
        "interval_seconds": 300,
        "active_start": "00:00",
        "active_end": "23:59",
        "timezone": "UTC",
    },
    # Deletes inbound channel event ids older than 7 days (shared state backend only).
    {
        "name": "sweep-channel-dedup",
        "kind": "system",
        "handler": "sweep_channel_dedup",
        "interval_seconds": 86400,
        "active_start": "00:00",
        "active_end": "23:59",
        "timezone": "UTC",
    },
]
Path(out).write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")
PY

# Merge by job name so jobs added in the admin console survive a re-run.
MERGE_CRON="${SCRIPT_DIR_LIB}/../ops/merge-cron-jobs.py"
if [[ "${TS_HARNESS_STATE_BACKEND:-local}" == "postgres" ]]; then
    echo "[phase:${PHASE_NAME}] Merging system jobs into the shared cron tree (Postgres) ..."
    MEMORY_API_TOKEN="${MEMORY_API_TOKEN:-}" python3 "$MERGE_CRON" --jobs "$SYSTEM_JOBS" \
        --memory-api "${MEMORY_API_URL:-http://127.0.0.1:18792}"
else
    echo "[phase:${PHASE_NAME}] Merging system jobs into $CRON_CONFIG ..."
    python3 "$MERGE_CRON" --jobs "$SYSTEM_JOBS" --file "$CRON_CONFIG"
    chown "${ADMIN_USER}:${ADMIN_USER}" "$CRON_CONFIG" 2>/dev/null || true
fi

echo "[phase:${PHASE_NAME}] Restarting agent-runtime to load scheduler config."
cd "$ENTERPRISE_ASSETS_DIR"
recreate_ts_harness_services agent-runtime memory-api

HEALTH_SCRIPT="${ENTERPRISE_ASSETS_DIR:-$HOME/enterprise-assets}/scripts/ops/gateway-health-check.sh"
METRICS_SCRIPT="${ENTERPRISE_ASSETS_DIR:-$HOME/enterprise-assets}/scripts/ops/publish-runtime-metrics.sh"
if [[ -f "$METRICS_SCRIPT" ]]; then
    echo "[phase:${PHASE_NAME}] Preparing runtime metrics publish script: $METRICS_SCRIPT"
    chmod +x "$METRICS_SCRIPT"
    CRON_LINE="*/2 * * * * ${HEALTH_CRON_PATH} AWS_REGION=${AWS_REGION:-us-east-1} GATEWAY_PORT=${GATEWAY_PORT:-18789} MEMORY_API_URL=${MEMORY_API_URL:-http://127.0.0.1:18792} $METRICS_SCRIPT >> /var/log/ts-harness-health.log 2>&1"
    EXISTING_CRON="$(crontab -l 2>/dev/null | grep -v "gateway-health-check" | grep -v "publish-runtime-metrics" || true)"
    printf '%s\n%s\n' "$EXISTING_CRON" "$CRON_LINE" | crontab -
    echo "[phase:${PHASE_NAME}] Registered runtime metrics publish cron (every 2 min)."
elif [[ -f "$HEALTH_SCRIPT" ]]; then
    echo "[phase:${PHASE_NAME}] Preparing runtime health check script: $HEALTH_SCRIPT"
    chmod +x "$HEALTH_SCRIPT"
    CRON_LINE="*/2 * * * * ${HEALTH_CRON_PATH} AWS_REGION=${AWS_REGION:-us-east-1} GATEWAY_PORT=${GATEWAY_PORT:-18789} $HEALTH_SCRIPT >> /var/log/ts-harness-health.log 2>&1"
    EXISTING_CRON="$(crontab -l 2>/dev/null | grep -v "gateway-health-check" || true)"
    printf '%s\n%s\n' "$EXISTING_CRON" "$CRON_LINE" | crontab -
    echo "[phase:${PHASE_NAME}] Registered runtime health check cron (every 2 min)."
else
    echo "[phase:${PHASE_NAME}] WARNING: gateway-health-check.sh not found at $HEALTH_SCRIPT — health cron not registered."
fi

mkdir -p "$DATA_DIR/.setup"
write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
