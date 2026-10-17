#!/usr/bin/env bash
# Post-deploy smoke checks for TS Harness.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GATEWAY_PORT="${GATEWAY_PORT:-18789}"
BASE_URL="${AGENT_RUNTIME_URL:-http://127.0.0.1:${GATEWAY_PORT}}"
RUN_GOOGLE=false
RUN_WORKFLOW=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [--google] [--workflow]

Runs required post-deploy health checks and optional live Google/workflow smokes.
Exits non-zero when a required check fails.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --google) RUN_GOOGLE=true ;;
        --workflow) RUN_WORKFLOW=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

echo "[post-deploy] Checking gateway health at ${BASE_URL}/healthz"
HTTP_CODE="$(curl -s -o /tmp/post-deploy-health.json -w '%{http_code}' --max-time 15 "${BASE_URL}/healthz" || echo 000)"
if [[ "$HTTP_CODE" != "200" ]]; then
    echo "[post-deploy] FAIL: /healthz returned HTTP ${HTTP_CODE}" >&2
    exit 1
fi
python3 - <<'PY'
import json, sys
payload = json.load(open("/tmp/post-deploy-health.json"))
if payload.get("status") != "ok":
    raise SystemExit("[post-deploy] FAIL: healthz status is not ok")
print("[post-deploy] OK: healthz")
PY

echo "[post-deploy] Checking /metrics baseline"
METRICS_CODE="$(curl -s -o /tmp/post-deploy-metrics.json -w '%{http_code}' --max-time 10 "${BASE_URL}/metrics" || echo 000)"
if [[ "$METRICS_CODE" != "200" ]]; then
    echo "[post-deploy] FAIL: /metrics returned HTTP ${METRICS_CODE}" >&2
    exit 1
fi
echo "[post-deploy] OK: /metrics"

echo "[post-deploy] Running release gates (post_deploy profile)"
(
    cd "$REPO_ROOT"
    export RELEASE_GATES_PROFILE=post_deploy
    export AGENT_RUNTIME_URL="$BASE_URL"
    python3 scripts/release-gates.py
)
echo "[post-deploy] OK: release gates"

if [[ "$RUN_GOOGLE" == "true" ]]; then
    echo "[post-deploy] Running Google live smokes (requires secrets and optional SMOKE_GMAIL_TO)"
    (
        cd "$REPO_ROOT"
        export RELEASE_GATES_PROFILE=post_deploy
        export AGENT_RUNTIME_URL="$BASE_URL"
        python3 scripts/release-gates.py
    )
fi

if [[ "$RUN_WORKFLOW" == "true" ]]; then
    echo "[post-deploy] Running PRD workflow live smoke via release gates"
    (
        cd "$REPO_ROOT"
        export RELEASE_GATES_PROFILE=post_deploy
        export AGENT_RUNTIME_URL="$BASE_URL"
        python3 scripts/release-gates.py
    )
fi

echo "[post-deploy] All requested checks passed."
exit 0
