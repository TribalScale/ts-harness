#!/usr/bin/env bash
# AWS implementation of the cloud adapter contract.
# Sourced by scripts/cloud/adapter.sh when CLOUD_PROVIDER=aws.
# shellcheck shell=bash

_cloud_aws_bin() {
    local bin="${AWS_BIN:-$(command -v aws || true)}"
    if [[ -z "$bin" && -x /usr/local/bin/aws ]]; then
        bin="/usr/local/bin/aws"
    fi
    printf '%s' "$bin"
}

cloud_validate_environment() {
    local region="${CLOUD_REGION:-${AWS_REGION:-}}"
    local bin
    if [[ -z "$region" ]]; then
        echo "ERROR: CLOUD_REGION (or AWS_REGION) must be set for AWS adapter" >&2
        return 1
    fi
    bin="$(_cloud_aws_bin)"
    if [[ -z "$bin" || ! -x "$bin" ]]; then
        echo "ERROR: aws CLI not found (expected on PATH or /usr/local/bin/aws)" >&2
        return 1
    fi
    if [[ -z "${SECRETS_PREFIX:-}" ]]; then
        echo "ERROR: SECRETS_PREFIX must be set for AWS adapter" >&2
        return 1
    fi
    return 0
}

# cloud_fetch_secret <name> <required|optional>
cloud_fetch_secret() {
    local secret_id="${1:?secret name required}"
    local mode="${2:-required}"
    local result ec=0
    local region="${CLOUD_REGION:-${AWS_REGION:-}}"
    local bin
    bin="$(_cloud_aws_bin)"
    if [[ -z "$bin" ]]; then
        echo "ERROR: aws CLI not found" >&2
        return 1
    fi
    # Avoid toggling shell errexit — a temporary `set -e` here would abort callers
    # that intentionally inspect a non-zero return (e.g. set +e; cloud_fetch_secret ...).
    result=$("$bin" secretsmanager get-secret-value \
        --region "$region" \
        --secret-id "${SECRETS_PREFIX}/${secret_id}" \
        --query 'SecretString' --output text 2>&1) || ec=$?
    if (( ec == 0 )); then
        printf '%s' "$result"
        return 0
    fi
    if [[ "$result" == *ResourceNotFoundException* ]]; then
        if [[ "$mode" == "optional" ]]; then
            echo "WARNING: Secret not found (optional): ${SECRETS_PREFIX}/${secret_id}" >&2
            printf '%s' ""
            return 0
        fi
        echo "ERROR: Required secret not found: ${SECRETS_PREFIX}/${secret_id}" >&2
        return 1
    fi
    echo "ERROR: Secrets Manager failed for ${SECRETS_PREFIX}/${secret_id}: ${result}" >&2
    return 1
}

# cloud_fetch_managed_db_credentials <credential_ref>
cloud_fetch_managed_db_credentials() {
    local credential_ref="${1:?credential_ref required}"
    local region="${CLOUD_REGION:-${AWS_REGION:-}}"
    local bin
    bin="$(_cloud_aws_bin)"
    if [[ -z "$bin" ]]; then
        echo "ERROR: aws CLI not found" >&2
        return 1
    fi
    "$bin" secretsmanager get-secret-value \
        --region "$region" \
        --secret-id "$credential_ref" \
        --query 'SecretString' --output text
}

# cloud_fetch_runtime_config
# Prints SSM ${SECRETS_PREFIX}/runtime-config. Fail closed when missing.
cloud_fetch_runtime_config() {
    local region="${CLOUD_REGION:-${AWS_REGION:-}}"
    local bin name result ec=0
    bin="$(_cloud_aws_bin)"
    if [[ -z "$bin" ]]; then
        echo "ERROR: aws CLI not found" >&2
        return 1
    fi
    if [[ -z "${SECRETS_PREFIX:-}" ]]; then
        echo "ERROR: SECRETS_PREFIX must be set to fetch runtime-config" >&2
        return 1
    fi
    name="/${SECRETS_PREFIX#/}/runtime-config"
    result=$("$bin" ssm get-parameter \
        --region "$region" \
        --name "$name" \
        --query 'Parameter.Value' \
        --output text 2>&1) || ec=$?
    if (( ec == 0 )); then
        printf '%s' "$result"
        return 0
    fi
    echo "ERROR: runtime-config parameter not found or unreadable: ${name}: ${result}" >&2
    return 1
}

# Normalize vol-0abc → vol0abc for NVMe by-id matching.
_cloud_aws_vol_id_compact() {
    local vol_id="$1"
    printf '%s' "${vol_id//-/}"
}

# cloud_resolve_data_device <resource_id> <device_hint>
# Prefer identity match against DATA_DISK_RESOURCE_ID / EBS volume id.
cloud_resolve_data_device() {
    local resource_id="${1:-}"
    local device_hint="${2:-}"
    local root_disk="" root_src b cand link real base compact expected

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

    if [[ -n "$resource_id" ]]; then
        compact="$(_cloud_aws_vol_id_compact "$resource_id")"
        shopt -s nullglob
        for link in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*; do
            base=$(basename "$link")
            if [[ "$base" == *"$compact"* ]]; then
                real=$(readlink -f "$link" 2>/dev/null || true)
                [[ -z "$real" || ! -b "$real" ]] && continue
                expected=$(basename "$real")
                if is_root_device "$expected"; then
                    echo "ERROR: data disk resource_id ${resource_id} resolved to root device ${real}" >&2
                    shopt -u nullglob
                    return 1
                fi
                echo "$real"
                shopt -u nullglob
                return 0
            fi
        done
        shopt -u nullglob
        echo "ERROR: No block device matched data_disk_resource_id=${resource_id} (compact=${compact})" >&2
        return 1
    fi

    # Legacy heuristic when resource id is unknown (pre-metadata installs).
    if [[ -n "$device_hint" && -b "$device_hint" ]]; then
        base=$(basename "$(readlink -f "$device_hint" 2>/dev/null || echo "$device_hint")")
        if is_root_device "$base"; then
            echo "ERROR: device_hint ${device_hint} points at root disk" >&2
            return 1
        fi
        echo "$(readlink -f "$device_hint" 2>/dev/null || echo "$device_hint")"
        return 0
    fi

    if [[ -b /dev/xvdf ]]; then
        echo /dev/xvdf
        return 0
    fi

    shopt -s nullglob
    for link in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_*; do
        real=$(readlink -f "$link" 2>/dev/null || true)
        [[ -z "$real" || ! -b "$real" ]] && continue
        base=$(basename "$real")
        is_root_device "$base" && continue
        echo "$real"
        shopt -u nullglob
        return 0
    done
    shopt -u nullglob

    for cand in /dev/nvme1n1 /dev/nvme2n1 /dev/nvme3n1; do
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
    local token instance_id
    if [[ -n "${INSTANCE_RESOURCE_ID:-}" ]]; then
        printf '%s' "$INSTANCE_RESOURCE_ID"
        return 0
    fi
    token="$(curl -fsS --max-time 3 -X PUT \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" \
        http://169.254.169.254/latest/api/token 2>/dev/null || true)"
    if [[ -n "$token" ]]; then
        instance_id="$(curl -fsS --max-time 3 \
            -H "X-aws-ec2-metadata-token: ${token}" \
            http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || true)"
        if [[ -n "$instance_id" ]]; then
            printf '%s' "$instance_id"
            return 0
        fi
    fi
    printf '%s' "unknown"
    return 0
}

# cloud_publish_runtime_metrics <json_file>
# JSON shape:
# {
#   "namespace": "TSHarness",
#   "dimensions": {"InstanceId": "i-xxx"},
#   "metrics": [{"name":"GatewayHealthy","value":1,"unit":"None"}]
# }
cloud_publish_runtime_metrics() {
    local json_file="${1:?json_file required}"
    local region="${CLOUD_REGION:-${AWS_REGION:-us-east-1}}"
    local bin namespace
    bin="$(_cloud_aws_bin)"
    if [[ ! -x "$bin" ]]; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) CloudWatch metric publish failed: aws CLI not found" >&2
        return 0
    fi
    if [[ ! -f "$json_file" ]]; then
        echo "ERROR: metrics json file not found: $json_file" >&2
        return 1
    fi
    namespace="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("namespace") or "TSHarness")' "$json_file")"
    python3 - "$json_file" "$bin" "$region" "$namespace" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

path, aws_bin, region, namespace = sys.argv[1:5]
payload = json.loads(Path(path).read_text(encoding="utf-8"))
dims = payload.get("dimensions") or {}
dim_args = []
for key, value in dims.items():
    dim_args.append(f"{key}={value}")
metrics = payload.get("metrics") or []
failures = []
for metric in metrics:
    name = metric.get("name")
    value = metric.get("value")
    unit = metric.get("unit") or "None"
    if name is None or value is None:
        continue
    cmd = [
        aws_bin,
        "cloudwatch",
        "put-metric-data",
        "--region",
        region,
        "--namespace",
        namespace,
        "--metric-name",
        str(name),
        "--value",
        str(value),
        "--unit",
        str(unit),
    ]
    if dim_args:
        cmd.extend(["--dimensions", ",".join(dim_args)])
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        failures.append(f"{name}: {exc.stderr or exc.stdout or exc}")
if failures:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = f"{ts} CloudWatch metric publish failed: {'; '.join(failures)}"
    print(msg, file=sys.stderr)
    try:
        with open("/var/log/ts-harness-health.log", "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass
sys.exit(0)
PY
}
