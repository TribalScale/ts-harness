#!/bin/bash
# Shared bootstrap runner: export-ready env is expected from the provider entrypoint
# (e.g. scripts/user-data.sh). Executes the ordered phase graph with existing
# per-phase markers under $DATA_DIR/.setup/phase-*.done.
set -euo pipefail

BOOTSTRAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${BOOTSTRAP_DIR}/phases.sh"

: "${DATA_DIR:=/vol/ts-harness-data}"
: "${SCRIPT_DIR:=${ENTERPRISE_ASSETS_DIR:-}/scripts}"
PHASES_DIR="${PHASES_DIR:-${SCRIPT_DIR}/phases}"

if [[ ! -d "$PHASES_DIR" ]]; then
    echo "ERROR: phases directory not found: $PHASES_DIR" >&2
    exit 1
fi

mkdir -p "$DATA_DIR/.setup"

# Resolve cloud-neutral aliases when common helpers are available.
if [[ -f "${PHASES_DIR}/lib/common.sh" ]]; then
    # shellcheck disable=SC1091
    source "${PHASES_DIR}/lib/common.sh"
    if declare -F resolve_cloud_neutral_env >/dev/null 2>&1; then
        resolve_cloud_neutral_env
    fi
    if declare -F fetch_secret >/dev/null 2>&1; then
        export -f fetch_secret is_blank_or_placeholder require_env write_env_var write_dotenv_var 2>/dev/null || true
        export -f enterprise_assets_commit calculate_phase_fingerprint phase_marker_value 2>/dev/null || true
        export -f phase_marker_matches write_phase_marker resolve_ebs_data_device 2>/dev/null || true
        export -f resolve_cloud_neutral_env is_managed_memory_backend ensure_cloud_adapter 2>/dev/null || true
    fi
    if declare -F ensure_cloud_adapter >/dev/null 2>&1; then
        ensure_cloud_adapter || true
    fi
fi

refresh_multi_agent_flag() {
    if [[ -f "$DATA_DIR/.setup/instance-env.sh" ]]; then
        # shellcheck disable=SC1091
        source "$DATA_DIR/.setup/instance-env.sh"
    fi
    MULTI_AGENT_ENABLED_BOOL=false
    if [[ "${MULTI_AGENT_ENABLED:-false}" != *__TF_*__* && "${MULTI_AGENT_ENABLED:-}" == "true" ]]; then
        MULTI_AGENT_ENABLED_BOOL=true
    fi
}

refresh_multi_agent_flag

run_phase() {
    local script_name="$1"
    local script_path="$PHASES_DIR/$script_name"
    if [[ ! -f "$script_path" ]]; then
        echo "ERROR: Phase script not found: $script_path" >&2
        exit 1
    fi
    chmod +x "$script_path"
    echo "------------------------------------------"
    echo ">>> Running phase: $script_name"
    echo "[bootstrap] marker dir: $DATA_DIR/.setup (phase-*.done)"
    # stdin carries the phase list; docker compose exec in a phase would drain it.
    "$script_path" </dev/null
}

while IFS= read -r phase_script; do
    [[ -n "$phase_script" ]] || continue
    refresh_multi_agent_flag
    if bootstrap_is_multi_agent_phase "$phase_script" && [[ "$MULTI_AGENT_ENABLED_BOOL" != "true" ]]; then
        echo "[bootstrap] Skipping multi-agent phase (disabled): $phase_script"
        continue
    fi
    run_phase "$phase_script"
done < <(bootstrap_phase_list)

echo "=========================================="
echo "TS Harness bootstrap phases finished."
echo "=========================================="
