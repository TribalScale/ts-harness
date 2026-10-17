#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

PHASE_NAME="10a-hydrate-runtime-config"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
RUNTIME_CONFIG_JSON="$DATA_DIR/.setup/runtime-config.json"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

require_env DATA_DIR
require_env SECRETS_PREFIX
resolve_cloud_neutral_env
ensure_cloud_adapter

echo "[phase:${PHASE_NAME}] Fetching runtime-config from ${CLOUD_PROVIDER:-aws}..."
RUNTIME_CONFIG="$(cloud_fetch_runtime_config)"
if [[ -z "${RUNTIME_CONFIG//[[:space:]]/}" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: runtime-config payload is empty" >&2
    exit 1
fi
mkdir -p "$DATA_DIR/.setup"
printf '%s\n' "$RUNTIME_CONFIG" > "$RUNTIME_CONFIG_JSON"
chmod 600 "$RUNTIME_CONFIG_JSON"

PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$RUNTIME_CONFIG_JSON" "$0")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current runtime-config. Skipping."
    exit 0
fi

eval "$(python3 - "$RUNTIME_CONFIG_JSON" <<'PY'
import json
import shlex
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(data, dict):
    raise SystemExit("runtime-config must be a JSON object")
for key, value in data.items():
    if not isinstance(key, str) or not key.isidentifier():
        continue
    if value is None:
        continue
    print(f"export {key}={shlex.quote(str(value))}")
PY
)"

write_instance_env
echo "[phase:${PHASE_NAME}] Wrote instance-env from runtime-config"

write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
