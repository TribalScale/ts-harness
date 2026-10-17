"""Prerequisite diagnostics for the TS Harness installer."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


# Stable CI/error codes (do not rename without updating callers/tests).
CODE_OK = "OK"
CODE_TERRAFORM_MISSING = "TERRAFORM_MISSING"
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
) -> list[CheckResult]:
    """Run installer prerequisite checks; return structured CheckResult list."""
    which = which or shutil.which
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


def preflight_failed(results: list[CheckResult]) -> bool:
    return any(not r.ok for r in results)


def format_check_results(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        mark = "OK" if r.ok else "FAIL"
        lines.append(f"[{mark}] {r.code}: {r.message}")
    return "\n".join(lines)
