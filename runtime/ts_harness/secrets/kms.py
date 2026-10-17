"""Provider-neutral credentials encryption backends (AWS KMS / GCP Cloud KMS / Fernet)."""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet


@runtime_checkable
class KmsBackend(Protocol):
    def encrypt(self, plaintext: str) -> str:
        """Return a transport-safe ciphertext string (typically base64)."""

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a ciphertext string produced by encrypt (or compatible legacy form)."""


def resolve_cloud_provider() -> str:
    raw = (
        os.environ.get("CLOUD_PROVIDER")
        or os.environ.get("TS_HARNESS_CLOUD_PROVIDER")
        or "aws"
    )
    return raw.strip().lower() or "aws"


def resolve_credentials_encryption_key_id() -> str:
    return (
        os.environ.get("CREDENTIALS_ENCRYPTION_KEY_ID")
        or os.environ.get("AGENT_CREDENTIALS_KMS_KEY_ID")
        or ""
    ).strip()


def _fernet_from_seed(seed: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest())
    return Fernet(key)


class FernetKmsBackend:
    """Local Fernet backend used for dev and as a fallback when cloud KMS is unavailable."""

    def __init__(self, seed: str | None = None) -> None:
        if seed is None:
            seed = os.environ.get(
                "TS_HARNESS_OAUTH_DEV_SECRET", "ts-harness-dev-oauth"
            )
        self._fernet = _fernet_from_seed(seed)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()


def looks_like_fernet(ciphertext: str) -> bool:
    # Fernet tokens are urlsafe-base64 and always begin with the version/timestamp
    # prefix "gAAAA". Do not treat short opaque KMS blobs as Fernet.
    return ciphertext.startswith("gAAAA")


def get_kms_backend(
    *,
    key_id: str | None = None,
    cloud_provider: str | None = None,
) -> KmsBackend:
    """Select encrypt/decrypt backend from CLOUD_PROVIDER and credentials key id."""
    resolved_key = (key_id if key_id is not None else resolve_credentials_encryption_key_id()).strip()
    provider = (cloud_provider or resolve_cloud_provider()).strip().lower() or "aws"

    if not resolved_key:
        # Prefer Fernet seeded by any configured key alias env for stable local crypto.
        seed = (
            os.environ.get("TS_HARNESS_OAUTH_DEV_SECRET")
            or "ts-harness-dev-oauth"
        )
        return FernetKmsBackend(seed)

    if provider == "gcp":
        from ts_harness.secrets.gcp_kms import GcpKmsBackend

        return GcpKmsBackend(resolved_key)

    from ts_harness.secrets.aws_kms import AwsKmsBackend

    return AwsKmsBackend(resolved_key)
