#!/usr/bin/env bash
# GCP implementation of the cloud adapter contract.
# Sourced by scripts/cloud/adapter.sh when CLOUD_PROVIDER=gcp.
# shellcheck shell=bash

_cloud_gcp_bin() {
    local bin="${GCLOUD_BIN:-$(command -v gcloud || true)}"
    if [[ -z "$bin" && -x /usr/bin/gcloud ]]; then
        bin="/usr/bin/gcloud"
    fi
    if [[ -z "$bin" && -x /usr/local/bin/gcloud ]]; then
        bin="/usr/local/bin/gcloud"
    fi
    printf '%s' "$bin"
}

# Logical "${SECRETS_PREFIX}/${name}" → Secret Manager id "${prefix}-${name}"
# (GCP secret_ids cannot contain '/'; see infra/gcp/secrets.tf).
_cloud_gcp_secret_id() {
    local name="${1:?secret name required}"
    local prefix="${SECRETS_PREFIX:-}"
    local secret_id
    if [[ "$name" == projects/*/secrets/* ]]; then
        # Full resource name: projects/P/secrets/ID[/versions/...]
        secret_id="${name#*/secrets/}"
        secret_id="${secret_id%%/*}"
        printf '%s' "$secret_id"
        return 0
    fi
    if [[ "$name" == *"/"* ]]; then
        secret_id="${name////-}"
    elif [[ -n "$prefix" ]]; then
        secret_id="${prefix}-${name}"
    else
        secret_id="$name"
    fi
    secret_id="${secret_id////-}"
    printf '%s' "$secret_id"
}

_cloud_gcp_project() {
    local project="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${GCLOUD_PROJECT:-}}}"
    if [[ -z "$project" ]]; then
        project="$(curl -fsS --max-time 2 \
            -H "Metadata-Flavor: Google" \
            http://metadata.google.internal/computeMetadata/v1/project/project-id 2>/dev/null || true)"
    fi
    printf '%s' "$project"
}

cloud_validate_environment() {
    local region="${CLOUD_REGION:-}"
    local bin project
    if [[ -z "$region" ]]; then
        echo "ERROR: CLOUD_REGION must be set for GCP adapter" >&2
        return 1
    fi
    bin="$(_cloud_gcp_bin)"
    if [[ -z "$bin" || ! -x "$bin" ]]; then
        echo "ERROR: gcloud CLI not found (expected on PATH or /usr/bin/gcloud)" >&2
        return 1
    fi
    if [[ -z "${SECRETS_PREFIX:-}" ]]; then
        echo "ERROR: SECRETS_PREFIX must be set for GCP adapter" >&2
        return 1
    fi
    project="$(_cloud_gcp_project)"
    if [[ -z "$project" ]]; then
        echo "ERROR: GCP_PROJECT_ID (or metadata project-id) must be set for GCP adapter" >&2
        return 1
    fi
    return 0
}

# cloud_fetch_secret <name> <required|optional>
cloud_fetch_secret() {
    local secret_name="${1:?secret name required}"
    local mode="${2:-required}"
    local result ec=0
    local bin project secret_id
    bin="$(_cloud_gcp_bin)"
    if [[ -z "$bin" ]]; then
        echo "ERROR: gcloud CLI not found" >&2
        return 1
    fi
    project="$(_cloud_gcp_project)"
    secret_id="$(_cloud_gcp_secret_id "$secret_name")"
    result=$("$bin" secrets versions access latest \
        --secret "$secret_id" \
        --project "$project" 2>&1) || ec=$?
    if (( ec == 0 )); then
        printf '%s' "$result"
        return 0
    fi
    if [[ "$result" == *"NOT_FOUND"* || "$result" == *"not found"* || "$result" == *"NotFound"* ]]; then
        if [[ "$mode" == "optional" ]]; then
            echo "WARNING: Secret not found (optional): ${secret_id}" >&2
            printf '%s' ""
            return 0
        fi
        echo "ERROR: Required secret not found: ${secret_id}" >&2
        return 1
    fi
    echo "ERROR: Secret Manager failed for ${secret_id}: ${result}" >&2
    return 1
}

# cloud_fetch_managed_db_credentials <credential_ref>
# credential_ref is typically projects/P/secrets/ID (or ID alone).
cloud_fetch_managed_db_credentials() {
    local credential_ref="${1:?credential_ref required}"
    local bin project secret_id
    bin="$(_cloud_gcp_bin)"
    if [[ -z "$bin" ]]; then
        echo "ERROR: gcloud CLI not found" >&2
        return 1
    fi
    project="$(_cloud_gcp_project)"
    secret_id="$(_cloud_gcp_secret_id "$credential_ref")"
    "$bin" secrets versions access latest \
        --secret "$secret_id" \
        --project "$project"
}

# cloud_fetch_runtime_config
# Prints Secret Manager ${SECRETS_PREFIX}-runtime-config. Fail closed when missing.
cloud_fetch_runtime_config() {
    local bin project secret_id result ec=0
    bin="$(_cloud_gcp_bin)"
    if [[ -z "$bin" ]]; then
        echo "ERROR: gcloud CLI not found" >&2
        return 1
    fi
    if [[ -z "${SECRETS_PREFIX:-}" ]]; then
        echo "ERROR: SECRETS_PREFIX must be set to fetch runtime-config" >&2
        return 1
    fi
    project="$(_cloud_gcp_project)"
    secret_id="$(_cloud_gcp_secret_id "runtime-config")"
    result=$("$bin" secrets versions access latest \
        --secret "$secret_id" \
        --project "$project" 2>&1) || ec=$?
    if (( ec == 0 )); then
        printf '%s' "$result"
        return 0
    fi
    echo "ERROR: runtime-config secret not found or unreadable: ${secret_id}: ${result}" >&2
    return 1
}

# cloud_resolve_data_device <resource_id> <device_hint>
# Prefer /dev/disk/by-id/google-* matching Persistent Disk / device_name.
cloud_resolve_data_device() {
    local resource_id="${1:-}"
    local device_hint="${2:-}"
    local root_disk="" root_src b cand link real base disk_name hint_base

    is_root_device() {
        local dev="$1"
        [[ -z "$root_disk" ]] && return 1
        [[ "$dev" == "$root_disk" ]] && return 0
        [[ "$dev" =~ ^${root_disk}p[0-9]+$ ]] && return 0
        return 1
    }

    root_src=$(findmnt -n -o SOURCE / 2>/dev/null || true)
    if [[ -n "$root_src" && -L "$root_src" ]]; then
        root_src=$(readlink -f "$root_src" 2>/dev/null || echo "$root_src")
    fi
    if [[ -n "$root_src" && -b "$root_src" ]]; then
        root_disk=$(lsblk -ndo PKNAME "$root_src" 2>/dev/null || true)
        if [[ -z "$root_disk" ]]; then
            b=$(basename "$root_src")
            if [[ "$b" =~ ^(nvme[0-9]+n[0-9]+)p[0-9]+$ ]]; then
                root_disk="${BASH_REMATCH[1]}"
            elif [[ "$b" =~ ^(.+)p[0-9]+$ ]]; then
                root_disk="${BASH_REMATCH[1]}"
            else
                root_disk="$b"
            fi
        fi
    fi

    disk_name=""
    if [[ -n "$resource_id" ]]; then
        disk_name="${resource_id##*/}"
        disk_name="${disk_name%%\?*}"
    fi

    # Prefer explicit device hint (infra sets /dev/disk/by-id/google-ts-harness-data).
    if [[ -n "$device_hint" ]]; then
        if [[ -b "$device_hint" || -L "$device_hint" ]]; then
            real=$(readlink -f "$device_hint" 2>/dev/null || echo "$device_hint")
            if [[ -b "$real" ]]; then
                base=$(basename "$real")
                if is_root_device "$base"; then
                    echo "ERROR: device_hint ${device_hint} points at root disk" >&2
                    return 1
                fi
                echo "$real"
                return 0
            fi
        fi
    fi

    if [[ -n "$disk_name" ]]; then
        shopt -s nullglob
        for link in /dev/disk/by-id/google-"${disk_name}" /dev/disk/by-id/google-*"${disk_name}"*; do
            [[ -e "$link" ]] || continue
            real=$(readlink -f "$link" 2>/dev/null || true)
            [[ -z "$real" || ! -b "$real" ]] && continue
            base=$(basename "$real")
            if is_root_device "$base"; then
                echo "ERROR: data disk resource_id ${resource_id} resolved to root device ${real}" >&2
                shopt -u nullglob
                return 1
            fi
            echo "$real"
            shopt -u nullglob
            return 0
        done
        shopt -u nullglob
        echo "ERROR: No block device matched data_disk_resource_id=${resource_id} (disk_name=${disk_name})" >&2
        return 1
    fi

    # Hint basename without requiring the link to exist yet (legacy).
    if [[ -n "$device_hint" ]]; then
        hint_base=$(basename "$device_hint")
        if [[ "$hint_base" == google-* ]]; then
            shopt -s nullglob
            for link in /dev/disk/by-id/"${hint_base}" /dev/disk/by-id/google-*; do
                [[ -e "$link" ]] || continue
                [[ "$(basename "$link")" == "$hint_base" || -z "$disk_name" ]] || continue
                real=$(readlink -f "$link" 2>/dev/null || true)
                [[ -z "$real" || ! -b "$real" ]] && continue
                base=$(basename "$real")
                is_root_device "$base" && continue
                echo "$real"
                shopt -u nullglob
                return 0
            done
            shopt -u nullglob
        fi
    fi

    shopt -s nullglob
    for link in /dev/disk/by-id/google-*; do
        # Skip typical boot disk serial symlinks when a dedicated data disk exists.
        real=$(readlink -f "$link" 2>/dev/null || true)
        [[ -z "$real" || ! -b "$real" ]] && continue
        base=$(basename "$real")
        is_root_device "$base" && continue
        echo "$real"
        shopt -u nullglob
        return 0
    done
    shopt -u nullglob

    for cand in /dev/sdb /dev/nvme0n2 /dev/nvme1n1; do
        if [[ -b "$cand" ]]; then
            base=$(basename "$cand")
            is_root_device "$base" && continue
            echo "$cand"
            return 0
        fi
    done
    return 1
}

cloud_get_instance_resource_id() {
    local instance_id
    if [[ -n "${INSTANCE_RESOURCE_ID:-}" ]]; then
        printf '%s' "$INSTANCE_RESOURCE_ID"
        return 0
    fi
    instance_id="$(curl -fsS --max-time 3 \
        -H "Metadata-Flavor: Google" \
        http://metadata.google.internal/computeMetadata/v1/instance/id 2>/dev/null || true)"
    if [[ -n "$instance_id" ]]; then
        printf '%s' "$instance_id"
        return 0
    fi
    printf '%s' "unknown"
    return 0
}

# cloud_publish_runtime_metrics <json_file>
# JSON shape matches AWS adapter; publishes custom.googleapis.com/<namespace>/<name>.
cloud_publish_runtime_metrics() {
    local json_file="${1:?json_file required}"
    local bin project
    bin="$(_cloud_gcp_bin)"
    project="$(_cloud_gcp_project)"
    if [[ -z "$bin" || ! -x "$bin" ]]; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Cloud Monitoring metric publish failed: gcloud CLI not found" >&2
        return 0
    fi
    if [[ -z "$project" ]]; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Cloud Monitoring metric publish failed: GCP project unknown" >&2
        return 0
    fi
    if [[ ! -f "$json_file" ]]; then
        echo "ERROR: metrics json file not found: $json_file" >&2
        return 1
    fi
    python3 - "$json_file" "$bin" "$project" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

path, gcloud_bin, project = sys.argv[1:4]
payload = json.loads(Path(path).read_text(encoding="utf-8"))
namespace = payload.get("namespace") or "TSHarness"
dims = payload.get("dimensions") or {}
instance_id = str(dims.get("InstanceId") or dims.get("instance_id") or "")
metrics = payload.get("metrics") or []
failures = []

# Resolve zone from metadata when available (required for gce_instance resource).
zone = ""
try:
    zone = subprocess.check_output(
        [
            "curl",
            "-fsS",
            "--max-time",
            "2",
            "-H",
            "Metadata-Flavor: Google",
            "http://metadata.google.internal/computeMetadata/v1/instance/zone",
        ],
        text=True,
    ).strip()
    if "/" in zone:
        zone = zone.rsplit("/", 1)[-1]
except Exception:
    zone = ""

def access_token() -> str:
    try:
        return subprocess.check_output(
            [gcloud_bin, "auth", "print-access-token"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        try:
            return subprocess.check_output(
                [
                    "curl",
                    "-fsS",
                    "--max-time",
                    "3",
                    "-H",
                    "Metadata-Flavor: Google",
                    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                ],
                text=True,
            )
            # metadata returns JSON {"access_token":"..."}
        except Exception as exc:
            raise RuntimeError(f"unable to obtain access token: {exc}") from exc

token_raw = ""
try:
    token_raw = access_token()
except Exception as exc:
    failures.append(str(exc))
    token_raw = ""

token = token_raw
if token_raw.startswith("{"):
    try:
        token = json.loads(token_raw).get("access_token") or ""
    except Exception:
        token = ""

if not token:
    failures.append("empty access token")
else:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_series = []
    for metric in metrics:
        name = metric.get("name")
        value = metric.get("value")
        if name is None or value is None:
            continue
        # Keep shared names: GatewayHealthy, WorkflowRunnerHealthy, WorkflowTasksPending
        metric_type = f"custom.googleapis.com/{namespace}/{name}"
        resource_labels = {"project_id": project}
        if instance_id:
            resource_labels["instance_id"] = instance_id
        if zone:
            resource_labels["zone"] = zone
        point = {
            "interval": {"endTime": now},
            "value": {"doubleValue": float(value)},
        }
        time_series.append(
            {
                "metric": {"type": metric_type},
                "resource": {"type": "gce_instance", "labels": resource_labels},
                "points": [point],
            }
        )
    if time_series:
        body = json.dumps({"timeSeries": time_series})
        url = f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
        try:
            proc = subprocess.run(
                [
                    "curl",
                    "-fsS",
                    "--max-time",
                    "15",
                    "-X",
                    "POST",
                    "-H",
                    f"Authorization: Bearer {token}",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    body,
                    url,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                failures.append(proc.stderr or proc.stdout or f"curl exit {proc.returncode}")
        except Exception as exc:
            failures.append(str(exc))

if failures:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"{ts} Cloud Monitoring metric publish failed: {'; '.join(failures)}"
    print(msg, file=sys.stderr)
    try:
        with open("/var/log/ts-harness-health.log", "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass
sys.exit(0)
PY
}
