#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"


PHASE_NAME="10b-render-workspace"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)


echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

EXAMPLE_CONFIG="$ENTERPRISE_ASSETS_DIR/config/organization.example.yaml"
CONFIG="${ORGANIZATION_CONFIG:-$ENTERPRISE_ASSETS_DIR/config/organization.yaml}"
if [[ -z "${ORGANIZATION_CONFIG:-}" && ! -f "$CONFIG" ]]; then
    echo "[phase:${PHASE_NAME}] WARNING: $CONFIG not found; rendering from $EXAMPLE_CONFIG." >&2
    echo "[phase:${PHASE_NAME}] Copy the example to config/organization.yaml or set ORGANIZATION_CONFIG." >&2
    CONFIG="$EXAMPLE_CONFIG"
fi
SOURCE_WORKSPACE="$ENTERPRISE_ASSETS_DIR/workspace"
RENDERED_WORKSPACE_DIR="$DATA_DIR/.setup/workspace-rendered"
RENDERER="$ENTERPRISE_ASSETS_DIR/scripts/render-workspace.py"

if [[ ! -f "$CONFIG" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: organization config not found: $CONFIG" >&2
    exit 1
fi
if [[ ! -d "$SOURCE_WORKSPACE" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: source workspace not found: $SOURCE_WORKSPACE" >&2
    exit 1
fi
if [[ ! -f "$RENDERER" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: renderer not found: $RENDERER" >&2
    exit 1
fi

OBJECT_BACKEND="${TS_HARNESS_OBJECT_BACKEND:-local}"
# A path that does not exist is hashed by name, so this re-runs the phase when the backend or bucket changes.
PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$CONFIG" "$SOURCE_WORKSPACE" "$RENDERER" \
    "object-backend:${OBJECT_BACKEND}:${TS_HARNESS_WORKSPACE_BUCKET:-}")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

chmod +x "$RENDERER"
python3 "$RENDERER" \
    --config "$CONFIG" \
    --src "$SOURCE_WORKSPACE" \
    --dst "$RENDERED_WORKSPACE_DIR" \
    --strict

chown -R "$ADMIN_USER:$ADMIN_USER" "$RENDERED_WORKSPACE_DIR"

if [[ "$OBJECT_BACKEND" == "s3" || "$OBJECT_BACKEND" == "gcs" ]]; then
    if [[ -z "${TS_HARNESS_WORKSPACE_BUCKET:-}" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: TS_HARNESS_OBJECT_BACKEND=$OBJECT_BACKEND but TS_HARNESS_WORKSPACE_BUCKET is empty" >&2
        exit 1
    fi
    echo "[phase:${PHASE_NAME}] Uploading missing workspace files to $OBJECT_BACKEND (existing objects are kept)"
    python3 "$ENTERPRISE_ASSETS_DIR/scripts/ops/seed-workspace-objects.py" \
        --src "$RENDERED_WORKSPACE_DIR" \
        --backend "$OBJECT_BACKEND" \
        --bucket "$TS_HARNESS_WORKSPACE_BUCKET"
fi

mkdir -p "$DATA_DIR/.setup"
write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
