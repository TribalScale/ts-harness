#!/usr/bin/env bash
# Switch where coding jobs run on this host without a Terraform apply:
#
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/coding-launcher.sh status
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/coding-launcher.sh local   # host containers
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/coding-launcher.sh ecs     # Fargate tasks
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/coding-launcher.sh clear   # back to runtime.coding_launcher
#
# The choice is a host override (DATA_DIR/.setup/coding-launcher.override) that wins over
# runtime.coding_launcher from the install config until cleared. Make the lasting change in the
# install config. The switch waits until no coding job is queued, starting or running (checked
# twice, 60 s apart; FORCE=1 skips the wait), then re-runs phase 12, which recreates only
# coding-launcher. ecs needs the Fargate stack (runtime.coding_workers_on_ecs, or scale_out).
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
COMPOSE_ENV="${COMPOSE_ENV:-$DATA_DIR/.setup/compose-env.sh}"
export DATA_DIR ADMIN_USER ENTERPRISE_ASSETS_DIR ENTERPRISE_DOCKER_DIR SCRIPT_DIR TS_HARNESS_CONFIG_DIR COMPOSE_ENV

# shellcheck disable=SC1091
source "$PHASES_DIR/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"

load_instance_env
override="$(coding_launcher_override_path)"
action="${1:-status}"

coding_jobs_active() {
    local runtime
    runtime="$(docker ps --filter "label=com.docker.compose.service=agent-runtime" --format '{{.Names}}' | head -1)"
    [[ -n "$runtime" ]] || return 1
    docker exec "$runtime" python3 -c '
import os, sys, psycopg
with psycopg.connect(os.environ["TS_HARNESS_SESSION_DB_URL"], connect_timeout=10) as c:
    n = c.execute("select count(*) from coding_jobs where status in (%s, %s, %s)", ("queued", "starting", "running")).fetchone()[0]
print(n)
sys.exit(0 if n else 1)
' >/dev/null 2>&1
}

wait_until_idle() {
    [[ "${FORCE:-0}" == "1" ]] && return 0
    local waited=0 timeout=$(( ${CODING_SWITCH_TIMEOUT_MINUTES:-120} * 60 ))
    while :; do
        if [[ -z "$(running_coding_workers)" ]] && ! coding_jobs_active; then
            sleep 60
            if [[ -z "$(running_coding_workers)" ]] && ! coding_jobs_active; then
                return 0
            fi
        fi
        if (( waited >= timeout )); then
            echo "ERROR: coding jobs still active after $((timeout / 60)) min; nothing changed (FORCE=1 to switch anyway)" >&2
            return 1
        fi
        (( waited % 600 == 0 )) && echo "Waiting for coding jobs to finish before switching (${waited}s)..."
        sleep 30
        waited=$((waited + 30))
    done
}

case "$action" in
    status)
        echo "coding launcher: $(resolve_coding_launcher)"
        echo "  host override:          $( [[ -f "$override" ]] && cat "$override" || echo none)"
        echo "  runtime.coding_launcher: ${CODING_LAUNCHER:-<unset>}"
        echo "  deployment profile:     ${DEPLOYMENT_PROFILE:-single_host}"
        echo "  ECS cluster:            ${CODING_ECS_CLUSTER:-<none>}"
        echo "  parallel limit:         ${CODING_MAX_RUNNING:-3}"
        exit 0
        ;;
    local|ecs|clear) ;;
    *)
        echo "usage: $0 status|local|ecs|clear" >&2
        exit 2
        ;;
esac

if [[ "$action" == "ecs" ]] && is_blank_or_placeholder "${CODING_ECS_CLUSTER:-}"; then
    echo "ERROR: no Fargate stack in the runtime config (CODING_ECS_CLUSTER is empty)." >&2
    echo "Set runtime.coding_workers_on_ecs: true in the install config and apply Terraform first." >&2
    exit 1
fi

wait_until_idle
if [[ "$action" == "clear" ]]; then
    rm -f "$override"
else
    mkdir -p "$(dirname "$override")"
    printf '%s\n' "$action" > "$override"
fi
echo "Coding launcher is now: $(resolve_coding_launcher)"
bash "$PHASES_DIR/12-compose-up.sh"
