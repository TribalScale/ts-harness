#!/usr/bin/env bash
# Durable installation identity under $DATA_DIR/.setup/install-metadata.json.
# shellcheck shell=bash

INSTALL_METADATA_CONTRACT_VERSION="${INSTALL_METADATA_CONTRACT_VERSION:-0.1.0}"

install_metadata_path() {
    echo "${DATA_DIR:?DATA_DIR must be set}/.setup/install-metadata.json"
}

install_metadata_exists() {
    [[ -f "$(install_metadata_path)" ]]
}

# Print a single field value (empty if missing).
install_metadata_get() {
    local key="${1:?key required}"
    local path
    path="$(install_metadata_path)"
    [[ -f "$path" ]] || return 0
    python3 - "$path" "$key" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)
value = data.get(key)
if value is None:
    sys.exit(0)
if isinstance(value, (dict, list)):
    print(json.dumps(value, separators=(",", ":")))
else:
    print(value)
PY
}

# Refuse CLOUD_PROVIDER that differs from recorded metadata.
install_metadata_assert_provider() {
    local wanted="${1:-${CLOUD_PROVIDER:-}}"
    local recorded
    recorded="$(install_metadata_get cloud_provider || true)"
    if [[ -n "$recorded" && -n "$wanted" && "$recorded" != "$wanted" ]]; then
        echo "ERROR: CLOUD_PROVIDER=${wanted} does not match install metadata cloud_provider=${recorded}" >&2
        echo "ERROR: Cross-provider reuse of $(install_metadata_path) is blocked." >&2
        return 1
    fi
    return 0
}

# Write/merge install metadata. Uses env vars when args omitted.
# Required semantic fields:
#   cloud_provider, account_or_project, cloud_region, environment,
#   compute_resource_id, data_disk_resource_id, terraform_state_identity,
#   install_contract_version
install_metadata_write() {
    local path
    path="$(install_metadata_path)"
    mkdir -p "$(dirname "$path")"
    CLOUD_PROVIDER="${CLOUD_PROVIDER:-}" \
    ACCOUNT_OR_PROJECT="${ACCOUNT_OR_PROJECT:-${AWS_ACCOUNT_ID:-${GCP_PROJECT_ID:-}}}" \
    CLOUD_REGION="${CLOUD_REGION:-${AWS_REGION:-}}" \
    ENVIRONMENT="${ENVIRONMENT:-${INSTALL_ENVIRONMENT:-}}" \
    COMPUTE_RESOURCE_ID="${COMPUTE_RESOURCE_ID:-${INSTANCE_RESOURCE_ID:-}}" \
    DATA_DISK_RESOURCE_ID="${DATA_DISK_RESOURCE_ID:-}" \
    TERRAFORM_STATE_IDENTITY="${TERRAFORM_STATE_IDENTITY:-}" \
    INSTALL_CONTRACT_VERSION="${INSTALL_CONTRACT_VERSION:-$INSTALL_METADATA_CONTRACT_VERSION}" \
    INSTALL_METADATA_PATH="$path" \
    python3 <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["INSTALL_METADATA_PATH"])
existing = {}
if path.exists():
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        existing = {}

def pick(key, *env_keys, default=""):
    if existing.get(key) not in (None, ""):
        # Prefer newly provided non-empty env over empty existing.
        pass
    for env_key in env_keys:
        value = os.environ.get(env_key, "")
        if value not in (None, ""):
            return value
    if existing.get(key) not in (None, ""):
        return existing[key]
    return default

payload = {
    "cloud_provider": pick("cloud_provider", "CLOUD_PROVIDER"),
    "account_or_project": pick("account_or_project", "ACCOUNT_OR_PROJECT"),
    "cloud_region": pick("cloud_region", "CLOUD_REGION"),
    "environment": pick("environment", "ENVIRONMENT"),
    "compute_resource_id": pick("compute_resource_id", "COMPUTE_RESOURCE_ID"),
    "data_disk_resource_id": pick("data_disk_resource_id", "DATA_DISK_RESOURCE_ID"),
    "terraform_state_identity": pick("terraform_state_identity", "TERRAFORM_STATE_IDENTITY"),
    "install_contract_version": pick(
        "install_contract_version",
        "INSTALL_CONTRACT_VERSION",
        default="0.1.0",
    ),
}
# Preserve unknown keys from prior writes (forward compatible).
for key, value in existing.items():
    if key not in payload:
        payload[key] = value

tmp = path.with_suffix(".tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
print(f"[install-metadata] wrote {path}")
PY
}
