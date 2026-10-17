#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/../bootstrap/install-metadata.sh"

ensure_cloud_adapter

PHASE_NAME="99-finalize"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
COMPLETION_MARKER="${COMPLETION_MARKER:-${DATA_DIR}/.setup-complete}"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

echo "[phase:${PHASE_NAME}] Writing completion marker..."

install_metadata_assert_provider "${CLOUD_PROVIDER:-aws}"
if [[ -z "${COMPUTE_RESOURCE_ID:-${INSTANCE_RESOURCE_ID:-}}" ]]; then
    COMPUTE_RESOURCE_ID="$(cloud_get_instance_resource_id || true)"
    INSTANCE_RESOURCE_ID="${INSTANCE_RESOURCE_ID:-$COMPUTE_RESOURCE_ID}"
fi
export COMPUTE_RESOURCE_ID="${COMPUTE_RESOURCE_ID:-${INSTANCE_RESOURCE_ID:-}}"
export INSTANCE_RESOURCE_ID
install_metadata_write

# Recompute provenance from the cloned enterprise assets repo (avoids inter-phase env coupling).
ENTERPRISE_REMOTE_URL="$(sudo -u "$ADMIN_USER" git -C "$ENTERPRISE_ASSETS_DIR" remote get-url origin 2>/dev/null || echo unknown)"
ENTERPRISE_ASSETS_COMMIT_CURRENT="$(sudo -u "$ADMIN_USER" git -C "$ENTERPRISE_ASSETS_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
TS_FQDN="$(cat "$DATA_DIR/.setup/tailscale-fqdn" 2>/dev/null || echo "")"
TS_FQDN="${TS_FQDN//[[:space:]]/}"
AGENT_RUNTIME_URL=""
if [[ -n "$TS_FQDN" ]]; then
    AGENT_RUNTIME_URL="https://${TS_FQDN}"
fi

cat > "$COMPLETION_MARKER" <<MARKER
setup_completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
agent_runtime_version=$AGENT_RUNTIME_VERSION
enterprise_remote_url=$ENTERPRISE_REMOTE_URL
enterprise_assets_commit=$ENTERPRISE_ASSETS_COMMIT_CURRENT
agent_runtime_url=$AGENT_RUNTIME_URL
admin_user=$ADMIN_USER
enterprise_memory_embedding_dim=${ENTERPRISE_MEMORY_EMBEDDING_DIM:-1536}
MARKER

chown "$ADMIN_USER:$ADMIN_USER" "$COMPLETION_MARKER"
echo "[phase:${PHASE_NAME}] Completion marker written to $COMPLETION_MARKER"
if [[ -n "$AGENT_RUNTIME_URL" ]]; then
    echo "[phase:${PHASE_NAME}] Agent runtime URL: $AGENT_RUNTIME_URL"
else
    echo "[phase:${PHASE_NAME}] Agent runtime URL unavailable (Tailscale FQDN not found)."
fi

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
