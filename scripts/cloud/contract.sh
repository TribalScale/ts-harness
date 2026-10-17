#!/usr/bin/env bash
# Cloud adapter operation contract.
# Provider adapters under scripts/cloud/{aws,gcp}/ must implement these names.
# The dispatcher (adapter.sh) sources exactly one provider based on CLOUD_PROVIDER.
#
# Operations:
#   cloud_validate_environment
#       Verify CLI/auth/region prerequisites for the selected provider.
#
#   cloud_fetch_secret <name> <required|optional>
#       Fetch secret string for "${SECRETS_PREFIX}/${name}".
#       required → fail closed when missing; optional → warn and print empty.
#
#   cloud_fetch_managed_db_credentials <credential_ref>
#       Fetch managed-DB credential payload (provider JSON) to stdout.
#
#   cloud_fetch_runtime_config
#       Print the Terraform-owned runtime-config JSON (Slack/OIDC/timezone knobs).
#       AWS: SSM /${SECRETS_PREFIX}/runtime-config. GCP: secret ${SECRETS_PREFIX}-runtime-config.
#       Fail closed when the payload is missing.
#
#   cloud_resolve_data_device <resource_id> <device_hint>
#       Resolve the dedicated data disk block device path to stdout.
#       Must refuse root disks and identity mismatches.
#
#   cloud_get_instance_resource_id
#       Print the compute instance resource id (EC2 instance-id / GCE id).
#
#   cloud_publish_runtime_metrics <json_file>
#       Publish metrics described by the JSON file to the provider monitor.
#
# Optional (only if shell remains responsible for credential crypto):
#   cloud_encrypt_credentials / cloud_decrypt_credentials

# shellcheck disable=SC2034
CLOUD_ADAPTER_OPERATIONS=(
    cloud_validate_environment
    cloud_fetch_secret
    cloud_fetch_managed_db_credentials
    cloud_fetch_runtime_config
    cloud_resolve_data_device
    cloud_get_instance_resource_id
    cloud_publish_runtime_metrics
)

CLOUD_ADAPTER_CONTRACT_VERSION="1"
