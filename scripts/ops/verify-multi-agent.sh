#!/usr/bin/env bash
set -euo pipefail

# Post-deploy smoke test for the TS Harness multi-agent topology.

ENT_DIR="${ENTERPRISE_ASSETS_DIR:-$HOME/enterprise-assets}"
ENT_DOCKER="${ENT_DIR}/docker"
MEMORY_BACKEND="${MEMORY_BACKEND:-local}"
AGENT_RUNTIME_URL="${AGENT_RUNTIME_URL:-http://127.0.0.1:18789}"
GATEWAY_TOKEN="${GATEWAY_TOKEN:-}"

EXPECTED_AGENTS=(
    orchestrator
    dev-agent
    ops-agent
    qa-agent
    review-agent
    analyst-agent
    pm-agent
    comms-agent
    marketing-agent
    sales-agent
    builder-agent
)

EXPECTED_CRONS=(
    daily-standup
    ci-pipeline-health
    overnight-alerts
    stale-pr-check
    sprint-progress-report
    weekly-merged-prs
    stale-ticket-alert
    cost-anomaly-check
    weekly-content-digest
    weekly-pipeline-report
    stale-deal-alert
    velocity-report
    memory-maintenance
    skills-audit
)

PASS=0
FAIL=0
failures=()

pass() { echo "  OK  $1"; (( PASS++ )) || true; }
fail() { echo "  FAIL $1"; failures+=("$1"); (( FAIL++ )) || true; }

compose_args=(-f "$ENT_DOCKER/docker-compose.memory-api.yml" -f "$ENT_DOCKER/docker-compose.agent-runtime.yml")
if [[ "$MEMORY_BACKEND" == "rds" || "$MEMORY_BACKEND" == "managed" ]]; then
    compose_args+=(-f "$ENT_DOCKER/docker-compose.managed-db.yml")
elif [[ -f "$ENT_DOCKER/docker-compose.local-db.yml" ]]; then
    compose_args+=(-f "$ENT_DOCKER/docker-compose.local-db.yml")
fi
if [[ "${WORKFLOW_RUNNER_ENABLED:-false}" == "true" && -f "$ENT_DOCKER/docker-compose.workflow-runner.yml" ]]; then
    compose_args+=(-f "$ENT_DOCKER/docker-compose.workflow-runner.yml")
fi

cd "$ENT_DIR"

echo "=== verify-multi-agent: TS Harness agent-runtime ==="
echo "=== compose: docker compose ${compose_args[*]} ==="

echo "--- Check 1: agent-runtime container reachable ---"
if docker compose "${compose_args[@]}" exec -T agent-runtime python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18789/healthz', timeout=5)" 2>/dev/null; then
    pass "agent-runtime container is running"
else
    fail "agent-runtime container not running or not reachable"
fi

echo "--- Check 2: configured topology has ${#EXPECTED_AGENTS[@]} agents ---"
python3 - "$ENT_DIR/config/ts-harness-runtime.yaml.tmpl" "${EXPECTED_AGENTS[@]}" <<'PY' && pass "all expected agents declared" || fail "agent topology mismatch"
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
expected = sys.argv[2:]
for agent in expected:
    if f"id: {agent}" not in text:
        raise SystemExit(f"missing {agent}")
PY

echo "--- Check 3: all 14 declared cron jobs are documented ---"
for job in "${EXPECTED_CRONS[@]}"; do
    if grep -R "$job" "$ENT_DIR/docs" "$ENT_DIR/workspace" >/dev/null 2>&1; then
        pass "cron '$job' documented"
    else
        fail "cron '$job' missing from docs/workspace"
    fi
done

echo "--- Check 4: workflow endpoint responds without crashing ---"
if [[ -n "$GATEWAY_TOKEN" ]]; then
    if curl -fsS -H "Authorization: Bearer $GATEWAY_TOKEN" -H "Content-Type: application/json" \
        -d '{"tenant_id":"default","agent_id":"orchestrator","task_id":"verify","input":{}}' \
        "$AGENT_RUNTIME_URL/workflow/dispatch" >/dev/null; then
        pass "workflow dispatch endpoint responded"
    else
        fail "workflow dispatch endpoint failed"
    fi
else
    echo "  SKIP workflow dispatch endpoint (GATEWAY_TOKEN not set)"
fi

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
    printf ' - %s\n' "${failures[@]}"
    exit 1
fi
