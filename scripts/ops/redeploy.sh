#!/usr/bin/env bash
# Roll the current enterprise-assets checkout onto this host.
#
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/redeploy.sh
#
# Pull the branch you want first. Running coding jobs are allowed to finish first: the launcher
# is paused and the deploy waits for ts-coding-* workers (CODING_DRAIN_TIMEOUT_MINUTES, default
# 120; FORCE_REDEPLOY=1 skips the wait and cuts them off).
# This re-fetches secrets (phase 10), re-hydrates the
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

current_branch="$(git -C "$ENTERPRISE_ASSETS_DIR" branch --show-current 2>/dev/null || true)"
echo "=== redeploy ${current_branch:-detached} $(git -C "$ENTERPRISE_ASSETS_DIR" log --oneline -1) ==="
if [[ -n "${ENTERPRISE_REPO_REF:-}" && "$current_branch" != "$ENTERPRISE_REPO_REF" ]]; then
    echo "WARNING: this host is configured to run '${ENTERPRISE_REPO_REF}' (runtime.enterprise_repo_ref) but '${current_branch:-detached}' is checked out." >&2
    echo "WARNING: fine for a live test; check out '${ENTERPRISE_REPO_REF}' and redeploy afterwards." >&2
fi
# Coding workers share agent-runtime's network: pause the launcher and let running jobs finish
# before anything is recreated. Phase 12 keeps the launcher stopped (HOLD_CODING_LAUNCHER) and
# the final `up -d` below starts it again.
drain_coding_workers redeploy
export HOLD_CODING_LAUNCHER=1
# A failed step must not leave the launcher stopped: queued coding jobs would never start.
resume_coding_launcher_on_failure() {
    local status=$?
    if (( status != 0 )); then
        local launcher
        launcher="$(coding_launcher_containers | head -1)"
        [[ -n "$launcher" ]] && docker start "$launcher" >/dev/null 2>&1 \
            && echo "redeploy failed (exit $status); the coding launcher was started again" >&2
    fi
    return "$status"
}
trap resume_coding_launcher_on_failure EXIT
for phase in 10-fetch-secrets 10a-hydrate-runtime-config 10b-render-workspace 11-agent-runtime-setup 11a-agents-init 12-compose-up 12b-github-webhook; do
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
# The coding worker image is built FROM ts-harness-agent-runtime:latest, and compose builds
# services in parallel (depends_on does not order builds). Build agent-runtime first so the
# worker never bakes in the previous runtime code.
run_docker_compose_as_admin build agent-runtime
run_docker_compose_as_admin build
echo "--- up"
# Starts the coding launcher again (phase 12 held it at zero).
HOLD_CODING_LAUNCHER=0 run_docker_compose_as_admin up -d
wait_for_agent_runtime_health redeploy
run_docker_compose_as_admin ps --format "table {{.Service}}\t{{.Status}}"

# The system cron jobs (defined in phase 15) reach the live list only through phase 15; it skips
# itself unless the phase script (so its job list), secrets or compose env changed.
echo "--- phase 15-automation-cron"
bash "$PHASES_DIR/15-automation-cron.sh"

# Each build leaves the previous images and their build cache behind; on a 40 GB root
# volume a day of redeploys filled the disk and the next image export failed with
# "no space left on device". Drop untagged images and keep the build cache under
# BUILD_CACHE_MAX (default 6 GB): enough for the next build to reuse its base layers.
echo "--- prune"
docker image prune -f >/dev/null || true
docker builder prune -f --max-used-space "${BUILD_CACHE_MAX:-6gb}" >/dev/null 2>&1 \
    || docker builder prune -f --filter "until=72h" >/dev/null || true
df -h / | tail -1
echo "=== redeploy done $(date -u +%FT%TZ) ==="
