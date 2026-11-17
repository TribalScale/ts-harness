"""Prerequisite diagnostics for the TS Harness installer."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


# Stable CI/error codes (do not rename without updating callers/tests).
CODE_OK = "OK"
CODE_TERRAFORM_MISSING = "TERRAFORM_MISSING"
CODE_TERRAFORM_TOO_OLD = "TERRAFORM_TOO_OLD"
CODE_SECRET_PREFIX_CHANGED = "SECRET_PREFIX_CHANGED"
CODE_AWS_CLI_MISSING = "AWS_CLI_MISSING"
CODE_GCLOUD_CLI_MISSING = "GCLOUD_CLI_MISSING"
CODE_CONFIG_IDENTITY_MISSING = "CONFIG_IDENTITY_MISSING"
CODE_CLOUD_FLAG_MISMATCH = "CLOUD_FLAG_MISMATCH"
CODE_STATE_PROVIDER_CONFLICT = "STATE_PROVIDER_CONFLICT"
CODE_INFRA_ROOT_MISSING = "INFRA_ROOT_MISSING"
CODE_CONFIG_INVALID = "CONFIG_INVALID"

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_CLI = {"aws": "aws", "gcp": "gcloud"}
PROVIDER_CLI_CODE = {
    "aws": CODE_AWS_CLI_MISSING,
    "gcp": CODE_GCLOUD_CLI_MISSING,
}


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str
    message: str


WhichFn = Callable[[str], Optional[str]]
VersionFn = Callable[[], Optional[tuple[int, ...]]]

SecretExistsFn = Callable[[str, str], Optional[bool]]

S3_NATIVE_LOCKING_MIN_TERRAFORM = (1, 10, 0)
# Always created by infra/aws/secrets.tf; used to detect which prefix an install uses.
PROBE_SECRET_KEY = "gateway-token"


def aws_secret_exists(secret_id: str, region: str) -> bool | None:
    """True/False when Secrets Manager answers; None when the lookup itself fails."""
    try:
        result = subprocess.run(
            ["aws", "secretsmanager", "describe-secret", "--secret-id", secret_id,
             "--region", region, "--output", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if "ResourceNotFoundException" in result.stderr:
        return False
    return None


def installed_terraform_version() -> tuple[int, ...] | None:
    try:
        out = subprocess.run(
            ["terraform", "version", "-json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        version = json.loads(out)["terraform_version"]
        return tuple(int(part) for part in version.split("-")[0].split("."))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def _ok(message: str) -> CheckResult:
    return CheckResult(True, CODE_OK, message)


def _fail(code: str, message: str) -> CheckResult:
    return CheckResult(False, code, message)


def read_state_metadata(state_dir: Path | None) -> dict[str, Any] | None:
    if state_dir is None:
        return None
    path = Path(state_dir) / "install-metadata.json"
    if not path.is_file():
        candidate = Path(state_dir)
        if candidate.name == "install-metadata.json" and candidate.is_file():
            path = candidate
        elif (candidate / "install-metadata.json").is_file():
            path = candidate / "install-metadata.json"
        else:
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_preflight(
    *,
    cloud: str,
    config: Mapping[str, Any],
    state_dir: Path | str | None = None,
    which: WhichFn | None = None,
    repo_root: Path | None = None,
    skip_schema: bool = False,
    terraform_version: VersionFn | None = None,
    secret_exists: SecretExistsFn | None = None,
) -> list[CheckResult]:
    """Run installer prerequisite checks; return structured CheckResult list."""
    which = which or shutil.which
    terraform_version = terraform_version or installed_terraform_version
    secret_exists = secret_exists or aws_secret_exists
    root = repo_root or REPO_ROOT
    results: list[CheckResult] = []

    provider = str(config.get("cloud_provider") or "").strip()
    if cloud not in ("aws", "gcp"):
        results.append(_fail(CODE_CONFIG_INVALID, f"Unsupported --cloud value: {cloud!r}"))
        return results

    if provider and provider != cloud:
        results.append(
            _fail(
                CODE_CLOUD_FLAG_MISMATCH,
                f"--cloud={cloud} does not match config cloud_provider={provider}",
            )
        )
    else:
        results.append(_ok(f"cloud flag matches config ({cloud})"))

    if which("terraform"):
        results.append(_ok("terraform binary present"))
        state = config.get("terraform_state") or {}
        if state.get("backend_type") == "s3" and not state.get("lock_table"):
            version = terraform_version()
            minimum = S3_NATIVE_LOCKING_MIN_TERRAFORM
            if version is not None and version < minimum:
                results.append(
                    _fail(
                        CODE_TERRAFORM_TOO_OLD,
                        "S3-native state locking needs terraform >= "
                        f"{'.'.join(map(str, minimum))} (found "
                        f"{'.'.join(map(str, version))}); upgrade terraform or set "
                        "terraform_state.lock_table to a DynamoDB table",
                    )
                )
    else:
        results.append(_fail(CODE_TERRAFORM_MISSING, "terraform binary not found on PATH"))

    cli = PROVIDER_CLI[cloud]
    if which(cli):
        results.append(_ok(f"{cli} CLI present"))
    else:
        results.append(
            _fail(
                PROVIDER_CLI_CODE[cloud],
                f"{cli} CLI not found on PATH (required for cloud={cloud})",
            )
        )

    infra = root / "infra" / cloud
    if infra.is_dir() and (infra / "variables.tf").is_file():
        results.append(_ok(f"terraform root present: infra/{cloud}"))
    else:
        results.append(
            _fail(CODE_INFRA_ROOT_MISSING, f"Missing terraform root at {infra}")
        )

    if cloud == "aws":
        identity = str(config.get("aws_account_id") or "").strip()
        if identity:
            results.append(_ok("aws_account_id present"))
        else:
            results.append(
                _fail(CODE_CONFIG_IDENTITY_MISSING, "aws_account_id is required for aws")
            )
        results.extend(_check_aws_secret_prefix(config, secret_exists))
    else:
        identity = str(config.get("gcp_project_id") or "").strip()
        if identity:
            results.append(_ok("gcp_project_id present"))
        else:
            results.append(
                _fail(CODE_CONFIG_IDENTITY_MISSING, "gcp_project_id is required for gcp")
            )

    if not skip_schema:
        try:
            from install_contract import InstallContractError, validate_install_config

            validate_install_config(config)
            results.append(_ok("install contract schema valid"))
        except InstallContractError as exc:
            results.append(_fail(CODE_CONFIG_INVALID, str(exc)))
        except Exception as exc:  # pragma: no cover - defensive
            results.append(_fail(CODE_CONFIG_INVALID, f"config validation error: {exc}"))

    state_path = Path(state_dir) if state_dir else None
    meta = read_state_metadata(state_path)
    if meta:
        recorded = str(meta.get("cloud_provider") or "").strip()
        if recorded and recorded != cloud:
            results.append(
                _fail(
                    CODE_STATE_PROVIDER_CONFLICT,
                    f"State dir records cloud_provider={recorded}, refusing {cloud}",
                )
            )
        else:
            results.append(_ok("state metadata provider compatible"))

    return results


def _check_aws_secret_prefix(
    config: Mapping[str, Any], secret_exists: SecretExistsFn
) -> list[CheckResult]:
    """Refuse to rename the secrets of an install created under project_name.

    AWS installs predating secret_prefix support always named secrets
    <project_name>/<key>. Switching prefix replaces every secret (populated values
    included) and the SSM runtime-config, while the running VM keeps the old prefix.
    """
    project = str(config.get("project_name") or "").strip()
    prefix = str(config.get("secret_prefix") or "").strip() or project
    if not project or prefix == project:
        return []
    region = str(config.get("cloud_region") or "")
    legacy_id = f"{project}/{PROBE_SECRET_KEY}"
    legacy = secret_exists(legacy_id, region)
    if legacy is None:
        return [_ok(f"secret_prefix {prefix!r} (could not look up {legacy_id}; check skipped)")]
    if legacy is False:
        return [_ok(f"secret_prefix {prefix!r} (no secrets under {project}/)")]
    if secret_exists(f"{prefix}/{PROBE_SECRET_KEY}", region) is True:
        return [_ok(f"secret_prefix {prefix!r} secrets present")]
    return [
        _fail(
            CODE_SECRET_PREFIX_CHANGED,
            f"Existing install uses secrets under {project}/ but secret_prefix is "
            f"{prefix!r}; applying would replace every secret. Set "
            f"secret_prefix: {project} in the install config.",
        )
    ]


def preflight_failed(results: list[CheckResult]) -> bool:
    return any(not r.ok for r in results)


def format_check_results(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        mark = "OK" if r.ok else "FAIL"
        lines.append(f"[{mark}] {r.code}: {r.message}")
    return "\n".join(lines)
