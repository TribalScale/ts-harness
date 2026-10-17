#!/usr/bin/env bash
# Cloud adapter dispatcher: source exactly one provider implementation.
# Requires CLOUD_PROVIDER=aws|gcp. Rejects unknown providers and provider
# changes recorded in $DATA_DIR/.setup/install-metadata.json.
# shellcheck shell=bash

_CLOUD_ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${_CLOUD_ADAPTER_DIR}/contract.sh"

_cloud_adapter_metadata_provider() {
    local meta="${DATA_DIR:-}/.setup/install-metadata.json"
    [[ -n "${DATA_DIR:-}" && -f "$meta" ]] || return 0
    python3 - "$meta" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(0)
provider = data.get("cloud_provider")
if provider:
    print(provider)
PY
}

_cloud_adapter_reject_provider_mismatch() {
    local recorded wanted
    recorded="$(_cloud_adapter_metadata_provider || true)"
    wanted="${CLOUD_PROVIDER:-}"
    if [[ -n "$recorded" && -n "$wanted" && "$recorded" != "$wanted" ]]; then
        echo "ERROR: CLOUD_PROVIDER=${wanted} does not match install metadata cloud_provider=${recorded}" >&2
        echo "ERROR: Cross-provider reuse of the same data directory is not supported; reinstall or use matching provider." >&2
        return 1
    fi
    return 0
}

: "${CLOUD_PROVIDER:=aws}"
export CLOUD_PROVIDER

case "$CLOUD_PROVIDER" in
    aws|gcp) ;;
    *)
        echo "ERROR: Unknown or unsupported CLOUD_PROVIDER='${CLOUD_PROVIDER}' (expected aws or gcp)" >&2
        return 1 2>/dev/null || exit 1
        ;;
esac

if ! _cloud_adapter_reject_provider_mismatch; then
    return 1 2>/dev/null || exit 1
fi

case "$CLOUD_PROVIDER" in
    aws)
        # shellcheck disable=SC1091
        source "${_CLOUD_ADAPTER_DIR}/aws/adapter.sh"
        ;;
    gcp)
        # shellcheck disable=SC1091
        source "${_CLOUD_ADAPTER_DIR}/gcp/adapter.sh"
        ;;
esac

# Ensure contract operations are present after provider load.
for _cloud_op in "${CLOUD_ADAPTER_OPERATIONS[@]}"; do
    if ! declare -F "$_cloud_op" >/dev/null 2>&1; then
        echo "ERROR: Provider adapter for CLOUD_PROVIDER=${CLOUD_PROVIDER} missing operation: ${_cloud_op}" >&2
        return 1 2>/dev/null || exit 1
    fi
done
unset _cloud_op

export CLOUD_ADAPTER_LOADED=1
