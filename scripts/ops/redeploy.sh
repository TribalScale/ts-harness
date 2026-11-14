#!/usr/bin/env bash
# Roll the current enterprise-assets checkout onto this host.
#
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/redeploy.sh
#
# Pull the branch you want first. This re-fetches secrets (phase 10), re-hydrates the
# runtime-config store (10a), re-renders the workspace, runtime YAML and agent docs (10b, 11,
# 11a), writes the compose env and starts the stack (12), applies database migrations, then
# rebuilds the images and recreates the containers whose image or settings changed.
#
# Each phase skips itself when its inputs have not changed, so running this after every pull
# is safe. A plain `docker compose build && up -d` is not enough: it misses template changes
# (models, prices), new secrets and new keys in the compose env.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
PHASES_DIR="$SCRIPT_DIR/phases"

if [[ -z "${ADMIN_USER:-}" ]]; then
    echo "ERROR: set ADMIN_USER (the host's admin account, which owns enterprise-assets)" >&2
    exit 1
fi
DATA_DIR="${DATA_DIR:-/vol/ts-harness-data}"
ENTERPRISE_ASSETS_DIR="${ENTERPRISE_ASSETS_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENTERPRISE_DOCKER_DIR="${ENTERPRISE_DOCKER_DIR:-$ENTERPRISE_ASSETS_DIR/docker}"
TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"
TS_HARNESS_WORKSPACE_DIR="${TS_HARNESS_WORKSPACE_DIR:-$TS_HARNESS_CONFIG_DIR/workspace}"
COMPOSE_ENV="${COMPOSE_ENV:-$DATA_DIR/.setup/compose-env.sh}"
SECRETS_ENV="${SECRETS_ENV:-$DATA_DIR/.setup/secrets.env}"
SECRETS_PREFIX="${SECRETS_PREFIX:-ts-harness}"
GATEWAY_PORT="${GATEWAY_PORT:-18789}"
export DATA_DIR ADMIN_USER ENTERPRISE_ASSETS_DIR ENTERPRISE_DOCKER_DIR SCRIPT_DIR
export TS_HARNESS_CONFIG_DIR TS_HARNESS_WORKSPACE_DIR COMPOSE_ENV SECRETS_PREFIX GATEWAY_PORT

# shellcheck disable=SC1091
source "$PHASES_DIR/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"

load_instance_env
resolve_cloud_neutral_env
export AWS_REGION="${AWS_REGION:-${CLOUD_REGION:-}}"

echo "=== redeploy $(git -C "$ENTERPRISE_ASSETS_DIR" log --oneline -1) ==="
for phase in 10-fetch-secrets 10a-hydrate-runtime-config 10b-render-workspace 11-agent-runtime-setup 11a-agents-init 12-compose-up; do
    echo "--- phase $phase"
    bash "$PHASES_DIR/$phase.sh"
    load_instance_env
done

set -a
# shellcheck disable=SC1090
source "$SECRETS_ENV"
# shellcheck disable=SC1090
source "$COMPOSE_ENV"
set +a
TS_HARNESS_SESSION_DB_URL="$(session_db_url)"
export TS_HARNESS_SESSION_DB_URL

echo "--- migrations"
bash "$OPS_DIR/migrate-db.sh"

reconstruct_compose_files
cd "$ENTERPRISE_ASSETS_DIR"
echo "--- build"
run_docker_compose_as_admin build
echo "--- up"
run_docker_compose_as_admin up -d
wait_for_agent_runtime_health redeploy
run_docker_compose_as_admin ps --format "table {{.Service}}\t{{.Status}}"
echo "=== redeploy done $(date -u +%FT%TZ) ==="
