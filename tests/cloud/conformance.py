"""Helpers for cross-provider (AWS/GCP) install conformance checks.

Builds a mocked normalized bootstrap environment from Terraform output
fixtures so shared Compose/bootstrap contracts can be asserted without
live cloud credentials.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from install_contract import NORMALIZED_OUTPUT_KEYS, normalize_install_outputs

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
AWS_OUTPUTS_FIXTURE = FIXTURES_DIR / "aws_terraform_outputs.json"
GCP_OUTPUTS_FIXTURE = FIXTURES_DIR / "gcp_terraform_outputs.json"

DATA_MOUNTPOINT = "/vol/ts-harness-data"
GATEWAY_PORT = 18789
MEMORY_API_PORT = 18792
HEALTH_PROBE_PATHS = (
    "/healthz",
    "/health",
)
COMPOSE_MANAGED_DB_FILES = (
    "docker-compose.memory-api.yml",
    "docker-compose.managed-db.yml",
    "docker-compose.agent-runtime.yml",
)
MANAGED_DB_ENV_KEYS = (
    "MANAGED_DB_HOST",
    "MANAGED_DB_PORT",
    "MANAGED_DB_NAME",
    "MANAGED_DB_USER",
    "MANAGED_DB_CREDENTIAL_REF",
)


def load_terraform_output_fixture(provider: str) -> dict[str, Any]:
    """Load mocked Terraform outputs for aws or gcp."""
    path = {
        "aws": AWS_OUTPUTS_FIXTURE,
        "gcp": GCP_OUTPUTS_FIXTURE,
    }.get(provider)
    if path is None:
        raise ValueError(f"Unsupported provider for conformance fixture: {provider!r}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Fixture {path} must be a JSON object")
    if data.get("cloud_provider") != provider:
        raise ValueError(
            f"Fixture {path} cloud_provider={data.get('cloud_provider')!r} "
            f"does not match requested {provider!r}"
        )
    return data


def logical_secret_catalog_keys(*, repo_root: Path | None = None) -> frozenset[str]:
    """Parse logical secret catalog keys from AWS secrets.tf (canonical catalog)."""
    root = repo_root or REPO_ROOT
    text = (root / "infra" / "aws" / "secrets.tf").read_text(encoding="utf-8")
    keys = set(re.findall(r'"([a-z0-9-]+)"\s*=\s*\{\s*consumers', text))
    if not keys:
        raise ValueError("Failed to parse logical secret catalog keys from infra/aws/secrets.tf")
    return frozenset(keys)


def gcp_logical_secret_catalog_keys(*, repo_root: Path | None = None) -> frozenset[str]:
    """Parse logical secret catalog keys from GCP secrets.tf."""
    root = repo_root or REPO_ROOT
    text = (root / "infra" / "gcp" / "secrets.tf").read_text(encoding="utf-8")
    keys = set(re.findall(r'"([a-z0-9-]+)"\s*=\s*\{\s*consumers', text))
    if not keys:
        raise ValueError("Failed to parse logical secret catalog keys from infra/gcp/secrets.tf")
    return frozenset(keys)


def select_compose_files(*, memory_backend: str = "managed") -> list[str]:
    """Return compose overlay basenames selected for a managed or local DB install."""
    files = ["docker-compose.memory-api.yml"]
    if memory_backend in ("rds", "cloudsql", "managed"):
        files.append("docker-compose.managed-db.yml")
    else:
        files.append("docker-compose.local-db.yml")
    files.append("docker-compose.agent-runtime.yml")
    return files


def build_normalized_bootstrap_env(
    raw_outputs: Mapping[str, Any],
    *,
    memory_backend: str = "managed",
) -> dict[str, Any]:
    """Normalize provider Terraform outputs into a shared bootstrap env dict.

    The returned mapping is what shared bootstrap/Compose selection depends on:
    CLOUD_PROVIDER, DATA_DIR, ports, MANAGED_DB_*, compose overlays, health probes,
    and the logical secret catalog (provider-neutral key names).
    """
    normalized = normalize_install_outputs(raw_outputs)
    provider = str(normalized["cloud_provider"])

    compose_files = select_compose_files(memory_backend=memory_backend)
    bootstrap: dict[str, Any] = {
        "CLOUD_PROVIDER": provider,
        "CLOUD_REGION": normalized["cloud_region"],
        "DATA_DIR": DATA_MOUNTPOINT,
        "data_mountpoint": DATA_MOUNTPOINT,
        "GATEWAY_PORT": GATEWAY_PORT,
        "MEMORY_API_PORT": MEMORY_API_PORT,
        "AGENT_RUNTIME_PORT": GATEWAY_PORT,
        "health_probe_paths": list(HEALTH_PROBE_PATHS),
        "compose_files": compose_files,
        "memory_backend": memory_backend,
        "SECRET_PREFIX": normalized["secret_prefix"],
        "secret_catalog_keys": sorted(logical_secret_catalog_keys()),
        "CREDENTIALS_ENCRYPTION_KEY_ID": normalized["credentials_encryption_key_id"],
        "MONITORING_NAMESPACE": normalized["monitoring_namespace"],
        "install_outputs": {key: normalized[key] for key in NORMALIZED_OUTPUT_KEYS},
    }

    for key in MANAGED_DB_ENV_KEYS:
        field = key.lower()
        bootstrap[key] = normalized.get(field)

    # Managed installs must populate DB connection fields used by compose phase.
    if memory_backend in ("rds", "cloudsql", "managed"):
        bootstrap["MEMORY_BACKEND"] = "rds" if provider == "aws" else "cloudsql"
    else:
        bootstrap["MEMORY_BACKEND"] = "local"

    return bootstrap


def bootstrap_env_from_fixture(provider: str, *, memory_backend: str = "managed") -> dict[str, Any]:
    """Load a provider fixture and build the normalized bootstrap env."""
    return build_normalized_bootstrap_env(
        load_terraform_output_fixture(provider),
        memory_backend=memory_backend,
    )


def cloud_neutral_runtime_surface(env: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the provider-neutral surface used for cross-provider equality."""
    return {
        "compose_files": list(env["compose_files"]),
        "GATEWAY_PORT": env["GATEWAY_PORT"],
        "MEMORY_API_PORT": env["MEMORY_API_PORT"],
        "AGENT_RUNTIME_PORT": env["AGENT_RUNTIME_PORT"],
        "data_mountpoint": env["data_mountpoint"],
        "DATA_DIR": env["DATA_DIR"],
        "health_probe_paths": list(env["health_probe_paths"]),
        "secret_catalog_keys": list(env["secret_catalog_keys"]),
        "MANAGED_DB_PORT": env.get("MANAGED_DB_PORT"),
        "MANAGED_DB_NAME": env.get("MANAGED_DB_NAME"),
    }
