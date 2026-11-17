"""Keep the install YAML next to the Terraform state, so a host can be rebuilt from any machine.

The YAML is stored as ``install-config.yaml`` beside the state object (S3: same key prefix;
GCS: inside the state prefix). ``install.sh apply`` uploads it after a successful apply, and
``install.sh config pull|push|diff`` manage it by hand. Install YAML holds secret *references*
(ARNs, secret ids), never secret values, so it can sit in the state bucket.
"""

from __future__ import annotations

import difflib
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

OBJECT_NAME = "install-config.yaml"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def stored_config_uri(config: Mapping[str, Any]) -> str:
    """Where this install's YAML is kept: beside the Terraform state."""
    state = config.get("terraform_state") or {}
    bucket = str(state.get("bucket") or "")
    key = str(state.get("key") or "").strip("/")
    if not bucket:
        raise ValueError("terraform_state.bucket is required to store the install config")
    if state.get("backend_type") == "gcs":
        prefix = key  # GCS backend "prefix" is a directory
        scheme = "gs"
    else:
        prefix = key.rsplit("/", 1)[0] if "/" in key else ""
        scheme = "s3"
    path = f"{prefix}/{OBJECT_NAME}" if prefix else OBJECT_NAME
    return f"{scheme}://{bucket}/{path}"


def _copy_cmd(src: str, dst: str, region: str = "") -> list[str]:
    if src.startswith("gs://") or dst.startswith("gs://"):
        return ["gcloud", "storage", "cp", "--quiet", src, dst]
    cmd = ["aws", "s3", "cp", "--only-show-errors", src, dst]
    if region:
        cmd += ["--region", region]
    return cmd


def _region(config: Mapping[str, Any]) -> str:
    return str((config.get("terraform_state") or {}).get("region") or "")


def push(config_path: Path, config: Mapping[str, Any], *, run: Runner = _run) -> str:
    uri = stored_config_uri(config)
    result = run(_copy_cmd(str(config_path), uri, _region(config)))
    if result.returncode != 0:
        raise RuntimeError(f"upload to {uri} failed: {(result.stderr or result.stdout).strip()[-300:]}")
    return uri


def fetch(uri: str, *, region: str = "", run: Runner = _run) -> str | None:
    """The stored YAML text, or None when nothing is stored yet."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / OBJECT_NAME
        result = run(_copy_cmd(uri, str(target), region))
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").lower()
            if "404" in message or "not found" in message or "does not exist" in message or "no urls matched" in message:
                return None
            raise RuntimeError(f"download of {uri} failed: {(result.stderr or result.stdout).strip()[-300:]}")
        return target.read_text(encoding="utf-8")


def diff(local_text: str, stored_text: str | None, *, uri: str) -> str:
    """Unified diff (stored -> local); empty when they match."""
    if stored_text is None:
        return f"nothing stored at {uri} yet\n"
    if stored_text == local_text:
        return ""
    return "".join(
        difflib.unified_diff(
            stored_text.splitlines(keepends=True),
            local_text.splitlines(keepends=True),
            fromfile=uri,
            tofile="local",
        )
    )
