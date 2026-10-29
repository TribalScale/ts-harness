#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

PHASE_NAME="12a-db-credential-watch"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

load_instance_env
resolve_cloud_neutral_env

WATCH_SCRIPT="$SCRIPT_DIR/ops/watch-db-credentials.sh"
PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$0" "$WATCH_SCRIPT" "managed:$(is_managed_memory_backend && echo yes || echo no)" "admin:${ADMIN_USER}")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

EXISTING_CRON="$(crontab -l 2>/dev/null | grep -v "watch-db-credentials" || true)"
if is_managed_memory_backend; then
    chmod +x "$WATCH_SCRIPT"
    CRON_LINE="*/5 * * * * PATH=/usr/local/bin:/usr/bin:/bin DATA_DIR=${DATA_DIR} ADMIN_USER=${ADMIN_USER} ENTERPRISE_ASSETS_DIR=${ENTERPRISE_ASSETS_DIR} TS_HARNESS_CONFIG_DIR=${TS_HARNESS_CONFIG_DIR} $WATCH_SCRIPT >> /var/log/ts-harness-db-credentials.log 2>&1"
    printf '%s\n%s\n' "$EXISTING_CRON" "$CRON_LINE" | crontab -
    echo "[phase:${PHASE_NAME}] Registered managed DB credential watch (every 5 min)."
else
    printf '%s\n' "$EXISTING_CRON" | crontab -
    echo "[phase:${PHASE_NAME}] Local DB backend; no credential watch."
fi

mkdir -p "$DATA_DIR/.setup"
write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
