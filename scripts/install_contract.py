"""Load and validate install-time cloud choice configuration.

The install contract selects exactly one provider (aws or gcp) and defines the
normalized inputs/outputs consumed by shared bootstrap and Compose runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "config" / "install-contract.schema.json"

ALLOWED_PROVIDERS = frozenset({"aws", "gcp"})
PROVIDER_STATE_BACKEND = {"aws": "s3", "gcp": "gcs"}

NORMALIZED_OUTPUT_KEYS = (
    "cloud_provider",
    "cloud_region",
    "compute_resource_id",
    "data_disk_resource_id",
    "data_disk_device_hint",
    "secret_prefix",
    "managed_db_host",
    "managed_db_port",
    "managed_db_name",
    "managed_db_user",
    "managed_db_credential_ref",
    "credentials_encryption_key_id",
    "runtime_private_url",
    "operator_connection_command",
    "monitoring_namespace",
    "notification_channel_id",
)


class InstallContractError(ValueError):
    """Raised when an install config fails schema or semantic validation."""


def _load_schema(schema_path: Path | None = None) -> dict[str, Any]:
    path = schema_path or DEFAULT_SCHEMA_PATH
    try:
        return json.loads(path.read_text())
    except OSError as exc:
        raise InstallContractError(f"Unable to read schema at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InstallContractError(f"Invalid JSON schema at {path}: {exc}") from exc


def load_install_config(
    path: str | Path,
    *,
    schema_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load YAML install config from disk and validate it."""
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text())
    except OSError as exc:
        raise InstallContractError(f"Unable to read config at {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise InstallContractError(f"Invalid YAML at {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise InstallContractError("Install config must be a YAML mapping/object")

    validate_install_config(
        raw,
        schema_path=Path(schema_path) if schema_path else None,
    )
    return raw


def validate_install_config(
    config: Mapping[str, Any],
    *,
    schema_path: Path | None = None,
) -> None:
    """Validate install config against JSON Schema and semantic rules."""
    schema = _load_schema(schema_path)
    try:
        jsonschema.validate(instance=dict(config), schema=schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        raise InstallContractError(f"Schema validation failed at {path}: {exc.message}") from exc

    provider = config.get("cloud_provider")
    if provider not in ALLOWED_PROVIDERS:
        raise InstallContractError(
            f"Unknown cloud_provider {provider!r}; allowed: {sorted(ALLOWED_PROVIDERS)}"
        )

    has_aws_account = bool(str(config.get("aws_account_id") or "").strip())
    has_gcp_project = bool(str(config.get("gcp_project_id") or "").strip())

    if provider == "aws":
        if has_gcp_project:
            raise InstallContractError(
                "Mixed-provider fields: cloud_provider=aws must not set gcp_project_id"
            )
        if not has_aws_account:
            raise InstallContractError("aws_account_id is required when cloud_provider=aws")
    elif provider == "gcp":
        if has_aws_account:
            raise InstallContractError(
                "Mixed-provider fields: cloud_provider=gcp must not set aws_account_id"
            )
        if not has_gcp_project:
            raise InstallContractError("gcp_project_id is required when cloud_provider=gcp")

    if provider == "gcp" and config.get("deployment_profile", "single_host") == "scale_out":
        raise InstallContractError(
            "deployment_profile=scale_out is only supported on aws; use single_host on gcp"
        )
    scale_out = config.get("scale_out") or {}
    if provider == "gcp" and scale_out:
        raise InstallContractError("scale_out settings are only supported on aws")
    if (
        config.get("deployment_profile", "single_host") == "scale_out"
        and scale_out.get("network_mode", "vpc") != "vpc"
    ):
        raise InstallContractError("deployment_profile=scale_out requires scale_out.network_mode=vpc")

    managed_db = config.get("managed_db")
    if not isinstance(managed_db, Mapping) or not managed_db:
        raise InstallContractError("managed_db configuration is required for install")

    state = config.get("terraform_state")
    if not isinstance(state, Mapping):
        raise InstallContractError("terraform_state location is required and must be unambiguous")

    backend = str(state.get("backend_type") or "").strip()
    bucket = str(state.get("bucket") or "").strip()
    key = str(state.get("key") or "").strip()
    expected_backend = PROVIDER_STATE_BACKEND[provider]

    if not bucket or not key:
        raise InstallContractError(
            "Ambiguous terraform state location: bucket and key are required"
        )
    if backend != expected_backend:
        raise InstallContractError(
            f"Ambiguous terraform state location: cloud_provider={provider} "
            f"requires backend_type={expected_backend!r}, got {backend!r}"
        )
    if backend == "s3" and not str(state.get("region") or "").strip():
        raise InstallContractError(
            "Ambiguous terraform state location: S3 backend requires region"
        )
    if backend != "s3" and state.get("lock_table"):
        raise InstallContractError(
            "terraform_state.lock_table is only valid for S3 backends"
        )

    runtime = config.get("runtime") or {}
    if isinstance(runtime, Mapping) and str(runtime.get("identity_provider") or "static") == "oidc":
        # The JWKS URL and login endpoints come from the issuer's discovery document when empty.
        for key in ("oidc_issuer", "oidc_audience", "oidc_client_id"):
            if not str(runtime.get(key) or "").strip():
                raise InstallContractError(
                    f"{key} is required when runtime.identity_provider=oidc"
                )


def normalize_install_outputs(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize provider-specific Terraform/output maps to install-contract keys."""
    if not isinstance(raw, Mapping):
        raise InstallContractError("Install outputs must be a mapping")

    provider = raw.get("cloud_provider")
    if provider not in ALLOWED_PROVIDERS:
        raise InstallContractError(
            "normalize_install_outputs requires cloud_provider of 'aws' or 'gcp'"
        )

    compute = (
        raw.get("compute_resource_id")
        or raw.get("instance_id")
        or raw.get("compute_instance_id")
    )
    disk = (
        raw.get("data_disk_resource_id")
        or raw.get("ebs_volume_id")
        or raw.get("data_disk_id")
    )
    db_host = raw.get("managed_db_host") or raw.get("rds_address")
    db_port = raw.get("managed_db_port") or raw.get("rds_port") or 5432
    db_name = raw.get("managed_db_name") or raw.get("rds_db_name")
    db_user = raw.get("managed_db_user") or raw.get("rds_username")
    db_cred = (
        raw.get("managed_db_credential_ref")
        or raw.get("rds_master_secret_arn")
    )
    key_id = (
        raw.get("credentials_encryption_key_id")
        or raw.get("agent_credentials_kms_key_alias")
        or raw.get("agent_credentials_kms_key_arn")
    )
    operator_cmd = (
        raw.get("operator_connection_command")
        or raw.get("ssh_command")
    )
    notification = (
        raw.get("notification_channel_id")
        or raw.get("notification_topic_arn")
    )

    normalized = {
        "cloud_provider": provider,
        "cloud_region": raw.get("cloud_region"),
        "compute_resource_id": compute,
        "data_disk_resource_id": disk,
        "data_disk_device_hint": raw.get("data_disk_device_hint"),
        "secret_prefix": raw.get("secret_prefix"),
        "managed_db_host": db_host,
        "managed_db_port": db_port,
        "managed_db_name": db_name,
        "managed_db_user": db_user,
        "managed_db_credential_ref": db_cred,
        "credentials_encryption_key_id": key_id,
        "runtime_private_url": raw.get("runtime_private_url"),
        "operator_connection_command": operator_cmd,
        "monitoring_namespace": raw.get("monitoring_namespace"),
        "notification_channel_id": notification,
    }

    missing = [k for k in ("cloud_region", "compute_resource_id") if not normalized.get(k)]
    if missing:
        raise InstallContractError(
            f"Incomplete install outputs; missing required keys: {', '.join(missing)}"
        )

    # Preserve key order for callers/tests.
    return {key: normalized[key] for key in NORMALIZED_OUTPUT_KEYS}
