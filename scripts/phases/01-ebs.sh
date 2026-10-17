#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/../bootstrap/install-metadata.sh"

ensure_cloud_adapter


PHASE_NAME="01-ebs"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

# Refuse cross-provider reuse of an existing data directory.
install_metadata_assert_provider "${CLOUD_PROVIDER:-aws}"

# Skip check: mount is idempotent — if DATA_DIR is already a mountpoint with the
# correct data device, later phases can still skip via their own markers.  We only
# skip the whole phase (format + mount + fstab) when the marker is present and the
# version matches.
if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        echo "[phase:${PHASE_NAME}] Already complete for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

DATA_DISK_RESOURCE_ID="${DATA_DISK_RESOURCE_ID:-}"
DATA_DISK_DEVICE_HINT="${DATA_DISK_DEVICE_HINT:-/dev/sdf}"

echo "[phase:${PHASE_NAME}] Waiting for data volume to attach (provider=${CLOUD_PROVIDER:-aws})..."
EBS_WAIT=0
EBS_MAX_WAIT=120
EBS_DEVICE=""
EBS_RESOLVE_RC=1
while (( EBS_WAIT < EBS_MAX_WAIT )); do
    set +e
    EBS_DEVICE="$(cloud_resolve_data_device "$DATA_DISK_RESOURCE_ID" "$DATA_DISK_DEVICE_HINT")"
    EBS_RESOLVE_RC=$?
    set -e
    if (( EBS_RESOLVE_RC == 0 )) && [[ -n "$EBS_DEVICE" ]]; then
        break
    fi
    sleep 5
    (( EBS_WAIT += 5 )) || true
    echo "[phase:${PHASE_NAME}] ...waited ${EBS_WAIT}s"
done

if (( EBS_RESOLVE_RC != 0 )) || [[ -z "$EBS_DEVICE" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: data volume not detected after ${EBS_MAX_WAIT}s" >&2
    if [[ -n "$DATA_DISK_RESOURCE_ID" ]]; then
        echo "[phase:${PHASE_NAME}] Expected provider identity data_disk_resource_id=${DATA_DISK_RESOURCE_ID} (hint=${DATA_DISK_DEVICE_HINT})" >&2
    else
        echo "[phase:${PHASE_NAME}] Expected /dev/xvdf or NVMe data volume (set DATA_DISK_RESOURCE_ID for identity-safe resolve)" >&2
    fi
    exit 1
fi
echo "[phase:${PHASE_NAME}] Detected data device: $EBS_DEVICE"

if [[ ! -b "$EBS_DEVICE" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: Block device missing: $EBS_DEVICE" >&2
    exit 1
fi

# Never format a device that is already mounted (anywhere).
if findmnt -n -S "$EBS_DEVICE" >/dev/null 2>&1; then
    MOUNTED_AT="$(findmnt -n -o TARGET -S "$EBS_DEVICE" 2>/dev/null || true)"
    if [[ "$MOUNTED_AT" != "$DATA_DIR" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: $EBS_DEVICE is already mounted at ${MOUNTED_AT:-unknown}; refusing mkfs." >&2
        exit 1
    fi
fi

FSTYPE=$(blkid -o value -s TYPE "$EBS_DEVICE" 2>/dev/null || true)
FSTYPE="${FSTYPE//$'\n'/}"
if [[ -n "$FSTYPE" ]]; then
    echo "[phase:${PHASE_NAME}] $EBS_DEVICE already has filesystem type $FSTYPE; skipping mkfs."
else
    # Format only an unformatted device whose provider identity already matched above.
    FILE_DESC=$(file -sL "$EBS_DEVICE" 2>/dev/null || true)
    if [[ "$FILE_DESC" != *"data"* && "$FILE_DESC" != *"empty"* && -n "$FILE_DESC" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: $EBS_DEVICE has unexpected content; refusing mkfs." >&2
        echo "[phase:${PHASE_NAME}]   file -sL: $FILE_DESC" >&2
        exit 1
    fi
    if [[ -n "$DATA_DISK_RESOURCE_ID" ]]; then
        echo "[phase:${PHASE_NAME}] Formatting unformatted device $EBS_DEVICE (identity=${DATA_DISK_RESOURCE_ID}) as ext4..."
    else
        echo "[phase:${PHASE_NAME}] Formatting $EBS_DEVICE as ext4..."
    fi
    mkfs.ext4 -L ts_harness-data "$EBS_DEVICE"
fi

mkdir -p "$DATA_DIR"

if ! mountpoint -q "$DATA_DIR"; then
    mount "$EBS_DEVICE" "$DATA_DIR"
    echo "[phase:${PHASE_NAME}] Mounted $EBS_DEVICE at $DATA_DIR"
else
    echo "[phase:${PHASE_NAME}] $DATA_DIR already mounted."
fi

EBS_UUID="$(blkid -s UUID -o value "$EBS_DEVICE")"
if ! grep -q "$EBS_UUID" /etc/fstab; then
    echo "UUID=$EBS_UUID $DATA_DIR ext4 defaults,nofail 0 2" >> /etc/fstab
    echo "[phase:${PHASE_NAME}] Added fstab entry for UUID=$EBS_UUID"
else
    echo "[phase:${PHASE_NAME}] fstab entry already exists for $EBS_UUID"
fi

# Persist installation identity after the data disk is mounted.
mkdir -p "$DATA_DIR/.setup"
if [[ -z "${COMPUTE_RESOURCE_ID:-${INSTANCE_RESOURCE_ID:-}}" ]]; then
    COMPUTE_RESOURCE_ID="$(cloud_get_instance_resource_id || true)"
    INSTANCE_RESOURCE_ID="${INSTANCE_RESOURCE_ID:-$COMPUTE_RESOURCE_ID}"
fi
export COMPUTE_RESOURCE_ID="${COMPUTE_RESOURCE_ID:-${INSTANCE_RESOURCE_ID:-}}"
export INSTANCE_RESOURCE_ID
export DATA_DISK_RESOURCE_ID
export DATA_DISK_DEVICE_HINT
install_metadata_write

printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
