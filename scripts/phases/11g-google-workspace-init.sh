#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

PHASE_NAME="11g-google-workspace-init"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"

if [[ -f "$PHASE_DONE" ]]; then
    echo "[phase:${PHASE_NAME}] Already complete. Skipping."
    exit 0
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

if [[ ! -f "$SECRETS_ENV" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $SECRETS_ENV not found" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$SECRETS_ENV"

if [[ -z "${GOOGLE_WORKSPACE_SA_JSON_PATH:-}" || ! -f "$GOOGLE_WORKSPACE_SA_JSON_PATH" ]]; then
    echo "[phase:${PHASE_NAME}] WARNING: No google-workspace-sa-json secret; skipping validation."
    write_phase_marker "$PHASE_DONE" "skipped-no-sa"
    exit 0
fi

export TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"

python3 - "$GOOGLE_WORKSPACE_SA_JSON_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
raw = path.read_text(encoding="utf-8").replace("\xa0", " ")
data = json.loads(raw)
for key in ("client_email", "private_key", "type"):
    if key not in data:
        raise SystemExit(f"missing key in service account JSON: {key}")
print(f"[validate] service account: {data.get('client_email')}")
PY

sync_google_workspace_sa_json "$GOOGLE_WORKSPACE_SA_JSON_PATH"

write_phase_marker "$PHASE_DONE" "google-workspace-sa-validated"
echo "[phase:${PHASE_NAME}] Done."
