#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

PHASE_NAME="11a-agents-init"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)


echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

AGENT_IDS=(orchestrator dev-agent ops-agent qa-agent review-agent analyst-agent pm-agent comms-agent marketing-agent sales-agent builder-agent)

# Skill categories per agent (space-separated list)
declare -A AGENT_SKILLS
AGENT_SKILLS[orchestrator]="orchestrator memory-api workflow cost-awareness audit-logging"
AGENT_SKILLS[dev-agent]="dev memory"
AGENT_SKILLS[ops-agent]="ops memory"
AGENT_SKILLS[qa-agent]="qa memory"
AGENT_SKILLS[review-agent]="review memory"
AGENT_SKILLS[analyst-agent]="memory"
AGENT_SKILLS[pm-agent]="pm memory-api"
AGENT_SKILLS[comms-agent]="comms memory-api"
AGENT_SKILLS[marketing-agent]="memory-api"
AGENT_SKILLS[sales-agent]="memory-api"
AGENT_SKILLS[builder-agent]="program-builder"

TS_HARNESS_AGENTS_BASE="${TS_HARNESS_CONFIG_DIR}/agents"
TS_HARNESS_WORKSPACE_BASE="${TS_HARNESS_CONFIG_DIR}"
RENDERED_WORKSPACE_DIR="$DATA_DIR/.setup/workspace-rendered"

if [[ ! -d "$RENDERED_WORKSPACE_DIR" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: rendered workspace not found at $RENDERED_WORKSPACE_DIR" >&2
    exit 1
fi

PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$RENDERED_WORKSPACE_DIR" "$ENTERPRISE_ASSETS_DIR/config/ts-harness-runtime.yaml.tmpl" "$ENTERPRISE_ASSETS_DIR/skills" "$0")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

for agent_id in "${AGENT_IDS[@]}"; do
    echo "[phase:${PHASE_NAME}] Materializing $agent_id ..."

    workspace_dir="$TS_HARNESS_WORKSPACE_BASE/workspace-${agent_id}"
    agent_dir="$TS_HARNESS_AGENTS_BASE/${agent_id}/agent"
    skills_dir="$workspace_dir/skills"

    mkdir -p "$workspace_dir" "$agent_dir" "$skills_dir"

    # Copy per-agent workspace docs
    src_workspace="$RENDERED_WORKSPACE_DIR/${agent_id}"
    if [[ -d "$src_workspace" ]]; then
        cp -r "$src_workspace/"* "$workspace_dir/" 2>/dev/null || true
        echo "[phase:${PHASE_NAME}]   Copied workspace for $agent_id"
    else
        echo "[phase:${PHASE_NAME}]   WARNING: no workspace dir for $agent_id at $src_workspace"
    fi
    ROOT_MEMORY="$RENDERED_WORKSPACE_DIR/MEMORY.md"
    if [[ "$agent_id" != "orchestrator" && -f "$ROOT_MEMORY" && ! -f "$workspace_dir/MEMORY.md" ]]; then
        cp "$ROOT_MEMORY" "$workspace_dir/MEMORY.md"
        echo "[phase:${PHASE_NAME}]   Copied shared MEMORY.md for $agent_id"
    fi

    # Copy scoped skills into agent workspace
    for skill_cat in ${AGENT_SKILLS[$agent_id]}; do
        src_skill="$ENTERPRISE_ASSETS_DIR/skills/${skill_cat}"
        if [[ -d "$src_skill" ]]; then
            mkdir -p "$skills_dir/${skill_cat}"
            cp -r "$src_skill/"* "$skills_dir/${skill_cat}/" 2>/dev/null || true
            echo "[phase:${PHASE_NAME}]   Copied skill: $skill_cat -> $agent_id"
        fi
    done

    chown -R "${ADMIN_USER}:${ADMIN_USER}" "$workspace_dir" "$agent_dir"
done

mkdir -p "$DATA_DIR/.setup"
write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
