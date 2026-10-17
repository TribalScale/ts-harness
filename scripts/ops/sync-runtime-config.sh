#!/usr/bin/env bash
# Pull mutable app knobs from the cloud runtime-config store, re-render YAML,
# and restart agent-runtime only (no compose recreate).
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
PHASES_DIR="$SCRIPT_DIR/phases"
DATA_DIR="${DATA_DIR:-/vol/ts-harness-data}"
ADMIN_USER="${ADMIN_USER:-artemis}"
ENTERPRISE_ASSETS_DIR="${ENTERPRISE_ASSETS_DIR:-/home/${ADMIN_USER}/enterprise-assets}"
ENTERPRISE_DOCKER_DIR="${ENTERPRISE_DOCKER_DIR:-$ENTERPRISE_ASSETS_DIR/docker}"
TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"
COMPOSE_ENV="${COMPOSE_ENV:-$DATA_DIR/.setup/compose-env.sh}"
SECRETS_ENV="${SECRETS_ENV:-$DATA_DIR/.setup/secrets.env}"

export SCRIPT_DIR DATA_DIR ADMIN_USER ENTERPRISE_ASSETS_DIR ENTERPRISE_DOCKER_DIR
export TS_HARNESS_CONFIG_DIR COMPOSE_ENV

# shellcheck disable=SC1091
source "$PHASES_DIR/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"

load_instance_env
resolve_cloud_neutral_env
ensure_cloud_adapter

echo "=== Sync runtime-config from ${CLOUD_PROVIDER:-aws} (${SECRETS_PREFIX}/runtime-config) ==="
RUNTIME_CONFIG="$(cloud_fetch_runtime_config)"
if [[ -z "${RUNTIME_CONFIG//[[:space:]]/}" ]]; then
    echo "ERROR: runtime-config payload is empty" >&2
    exit 1
fi

echo "Hydrating instance-env (phase 10a)..."
bash "$PHASES_DIR/10a-hydrate-runtime-config.sh"
load_instance_env

echo "Re-rendering runtime YAML (phase 11)..."
bash "$PHASES_DIR/11-agent-runtime-setup.sh"

if [[ -f "$SECRETS_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$SECRETS_ENV"
fi
if [[ -f "$COMPOSE_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$COMPOSE_ENV"
    set +a
fi
reconstruct_compose_files

cd "$ENTERPRISE_DOCKER_DIR"
echo "Restarting agent-runtime..."
restart_agent_runtime
echo "Done. Confirm /admin/auth/config still reports the expected identity mode."
