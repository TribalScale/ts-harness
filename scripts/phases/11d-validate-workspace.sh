#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

PHASE_NAME="11d-validate-workspace"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)

# This phase is not skipped based on version — always re-validate to catch post-boot edits.
# An operator can force a skip by creating the marker manually.
if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        echo "[phase:${PHASE_NAME}] Already validated for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

VALIDATOR="$ENTERPRISE_ASSETS_DIR/scripts/validate-placeholders.sh"
if [[ ! -f "$VALIDATOR" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: validate-placeholders.sh not found at $VALIDATOR" >&2
    exit 1
fi
chmod +x "$VALIDATOR"

FAILED=0
AGENT_IDS=(orchestrator dev-agent ops-agent qa-agent review-agent analyst-agent pm-agent comms-agent marketing-agent sales-agent builder-agent)

# ---- Scan 1: Source config dirs (excluding *.tmpl — those are templates rendered elsewhere) ---
echo "[phase:${PHASE_NAME}] Scan 1: source config files ..."
CONFIG_SRC="$ENTERPRISE_ASSETS_DIR/config"
if [[ -d "$CONFIG_SRC" ]]; then
    MATCHES=$(grep -RIEn \
        --include='*.json' --include='*.yaml' --include='*.yml' --include='*.env' \
        '\{\{[A-Z][A-Z0-9_]*\}\}' "$CONFIG_SRC" 2>/dev/null || true)
    if [[ -n "$MATCHES" ]]; then
        echo "$MATCHES" | sed "s|^|[phase:${PHASE_NAME}]   |"
        echo "[phase:${PHASE_NAME}] ERROR: Unfilled placeholders in source config dir: $CONFIG_SRC" >&2
        FAILED=1
    else
        echo "[phase:${PHASE_NAME}]   No unfilled placeholders in non-template config files."
    fi
fi

# ---- Scan 2: Rendered (EBS) workspace dirs — all file types ----------------
# After bootstrap renders workspace files, ALL {{PLACEHOLDERS}} must be filled.
echo "[phase:${PHASE_NAME}] Scan 2: rendered workspace dirs ..."
for agent_id in "${AGENT_IDS[@]}"; do
    rendered_dir="$DATA_DIR/.ts-harness/workspace-${agent_id}"
    if [[ -d "$rendered_dir" ]]; then
        echo "[phase:${PHASE_NAME}]   Scanning rendered: $rendered_dir"
        if ! bash "$VALIDATOR" "$rendered_dir" 2>&1 | sed "s|^|[phase:${PHASE_NAME}]   |"; then
            echo "[phase:${PHASE_NAME}] ERROR: Unfilled placeholders in rendered workspace: $rendered_dir" >&2
            FAILED=1
        fi
    fi
done

if (( FAILED )); then
    echo "[phase:${PHASE_NAME}] FATAL: Unfilled {{PLACEHOLDER}} tokens found. Fill all tokens via Terraform variables before deploying." >&2
    exit 1
fi

echo "[phase:${PHASE_NAME}] All workspace validation checks passed."

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
