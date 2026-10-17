#!/usr/bin/env bash
# Probe the gateway healthcheck endpoint and publish a custom metric via cloud adapter.
# Designed to run as a cron job every 2 minutes on the instance.
#
# Required env vars (set in crontab or sourced from environment):
#   CLOUD_REGION / AWS_REGION - cloud region (default: us-east-1)
#   GATEWAY_PORT              - gateway HTTP port (default: 18789)
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/phases/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/phases/lib/common.sh"

ensure_cloud_adapter

GATEWAY_PORT="${GATEWAY_PORT:-18789}"
REGION="${CLOUD_REGION:-${AWS_REGION:-us-east-1}}"
export CLOUD_REGION="$REGION"
export AWS_REGION="${AWS_REGION:-$REGION}"

# Prefer adapter (IMDSv2 on AWS); keep AWS_BIN hint for ops diagnostics.
AWS_BIN="${AWS_BIN:-$(command -v aws || true)}"
if [[ -z "$AWS_BIN" && -x /usr/local/bin/aws ]]; then
    AWS_BIN="/usr/local/bin/aws"
fi
export AWS_BIN

INSTANCE_ID="$(cloud_get_instance_resource_id)"

HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' \
    --max-time 10 \
    "http://127.0.0.1:${GATEWAY_PORT}/healthz" 2>/dev/null || echo 000)"

if [[ "$HTTP_CODE" == "200" ]]; then
    HEALTHY=1
else
    HEALTHY=0
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) UNHEALTHY: gateway returned HTTP $HTTP_CODE" \
        >> /var/log/ts-harness-health.log
fi

METRICS_JSON="$(mktemp)"
trap 'rm -f "$METRICS_JSON"' EXIT
cat > "$METRICS_JSON" <<EOF
{"namespace":"TSHarness","dimensions":{"InstanceId":"${INSTANCE_ID}"},"metrics":[{"name":"GatewayHealthy","value":${HEALTHY},"unit":"None"}]}
EOF

# Publish via adapter (CloudWatch put-metric-data on AWS). Keep cron non-fatal.
if ! cloud_publish_runtime_metrics "$METRICS_JSON"; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) CloudWatch metric publish failed: adapter publish returned error" \
        >> /var/log/ts-harness-health.log
fi
