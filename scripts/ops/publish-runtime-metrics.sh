#!/usr/bin/env bash
# Publish gateway + workflow-runner runtime metrics via the cloud adapter.
# Designed to run as a cron job every 2 minutes on the instance.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/phases/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/phases/lib/common.sh"

ensure_cloud_adapter

GATEWAY_PORT="${GATEWAY_PORT:-18789}"
MEMORY_API_URL="${MEMORY_API_URL:-http://127.0.0.1:18792}"
REGION="${CLOUD_REGION:-${AWS_REGION:-us-east-1}}"
export CLOUD_REGION="$REGION"
export AWS_REGION="${AWS_REGION:-$REGION}"
RUNNER_TICK_FILE="${WORKFLOW_RUNNER_TICK_FILE:-/tmp/workflow-runner.tick}"
RUNNER_METRICS_FILE="${WORKFLOW_RUNNER_METRICS_FILE:-/tmp/workflow-runner.metrics.json}"
RUNNER_STALE_SECONDS="${WORKFLOW_RUNNER_STALE_SECONDS:-360}"

INSTANCE_ID="$(cloud_get_instance_resource_id)"
INSTANCE_RESOURCE_ID="${INSTANCE_RESOURCE_ID:-$INSTANCE_ID}"

HEALTH_BODY="$(mktemp)"
METRICS_JSON="$(mktemp)"
SCALE_SNAPSHOT="$(mktemp)"
SCALE_JSON="$(mktemp)"
trap 'rm -f "$HEALTH_BODY" "$METRICS_JSON" "$SCALE_SNAPSHOT" "$SCALE_JSON"' EXIT

# Cron has no secrets in its environment; the runtime env file has the memory API token.
if [[ -z "${MEMORY_API_TOKEN:-}" && -r "${TS_HARNESS_CONFIG_DIR:-}/.env" ]]; then
    MEMORY_API_TOKEN="$(sed -n 's/^MEMORY_API_TOKEN="\(.*\)"$/\1/p' "$TS_HARNESS_CONFIG_DIR/.env" | head -1)"
fi

memory_curl() {
    if [[ -n "${MEMORY_API_TOKEN:-}" ]]; then
        curl -fsS --max-time 10 -H @<(printf 'Authorization: Bearer %s\n' "$MEMORY_API_TOKEN") "$@"
    else
        curl -fsS --max-time 10 "$@"
    fi
}

HTTP_CODE="$(curl -s -o "$HEALTH_BODY" -w '%{http_code}' \
    --max-time 10 \
    "http://127.0.0.1:${GATEWAY_PORT}/healthz" 2>/dev/null || echo 000)"
if [[ "$HTTP_CODE" == "200" ]]; then
    GATEWAY_HEALTHY=1
else
    GATEWAY_HEALTHY=0
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) UNHEALTHY: gateway returned HTTP $HTTP_CODE" \
        >> /var/log/ts-harness-health.log
fi

# Empty when /healthz is unreachable or lacks the component; the alarm treats missing data as breaching.
STALE_JOBS=""
if [[ "$HTTP_CODE" == "200" ]]; then
    STALE_JOBS="$(python3 - "$HEALTH_BODY" <<'PY'
import json, sys
from pathlib import Path
try:
    component = json.loads(Path(sys.argv[1]).read_text())["components"]["scheduler_jobs"]
    print(int(component["stale_count"]))
except Exception:
    print("")
PY
)"
fi

RUNNER_HEALTHY=0
if [[ -f "$RUNNER_TICK_FILE" ]]; then
    NOW="$(date +%s)"
    TICK_MTIME="$(stat -f %m "$RUNNER_TICK_FILE" 2>/dev/null || stat -c %Y "$RUNNER_TICK_FILE" 2>/dev/null || echo 0)"
    AGE=$((NOW - TICK_MTIME))
    if [[ "$AGE" -le "$RUNNER_STALE_SECONDS" ]]; then
        RUNNER_HEALTHY=1
    fi
fi

PENDING_TASKS=0
PENDING_PAYLOAD="$(memory_curl \
    -H "Content-Type: application/json" \
    -d '{"limit":500}' \
    "${MEMORY_API_URL%/}/workflow/ready" 2>/dev/null || true)"
if [[ -n "$PENDING_PAYLOAD" ]]; then
    PENDING_TASKS="$(python3 - <<'PY' "$PENDING_PAYLOAD"
import json, sys
try:
    payload = json.loads(sys.argv[1])
    tasks = payload.get("tasks") or []
    print(len(tasks))
except Exception:
    print(0)
PY
)"
fi

FAILED_COUNT=""
if [[ -f "$RUNNER_METRICS_FILE" ]]; then
    FAILED_COUNT="$(python3 - <<'PY' "$RUNNER_METRICS_FILE"
import json, sys
from pathlib import Path
try:
    payload = json.loads(Path(sys.argv[1]).read_text())
    print(int(payload.get("runner_tasks_failed") or 0))
except Exception:
    print(0)
PY
)"
fi

python3 - "$METRICS_JSON" "$INSTANCE_ID" "$GATEWAY_HEALTHY" "$RUNNER_HEALTHY" "$PENDING_TASKS" "${FAILED_COUNT}" "${STALE_JOBS}" <<'PY'
import json
import sys
from pathlib import Path

path, instance_id, gateway, runner, pending, failed, stale = sys.argv[1:8]
metrics = [
    {"name": "GatewayHealthy", "value": int(gateway), "unit": "None"},
    {"name": "WorkflowRunnerHealthy", "value": int(runner), "unit": "None"},
    {"name": "WorkflowTasksPending", "value": int(pending), "unit": "None"},
]
if failed != "":
    metrics.append({"name": "WorkflowTasksFailed", "value": int(failed), "unit": "None"})
if stale != "":
    metrics.append({"name": "SystemJobsStale", "value": int(stale), "unit": "None"})
payload = {
    "namespace": "TSHarness",
    "dimensions": {"InstanceId": instance_id},
    "metrics": metrics,
}
Path(path).write_text(json.dumps(payload), encoding="utf-8")
PY

cloud_publish_runtime_metrics "$METRICS_JSON"

# Cluster-wide lanes, coding jobs, gate waits, GitHub quota and LLM spend. No
# InstanceId dimension: every host reports the same database-backed series.
if memory_curl "${MEMORY_API_URL%/}/metrics/scale-out?window_seconds=120" > "$SCALE_SNAPSHOT" 2>/dev/null; then
    python3 "$OPS_DIR/scale_metrics_payload.py" "$SCALE_SNAPSHOT" "$SCALE_JSON" "$INSTANCE_ID" \
        && cloud_publish_runtime_metrics "$SCALE_JSON"
else
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) scale-out metrics unavailable from memory API" >> /var/log/ts-harness-health.log
fi
exit 0
