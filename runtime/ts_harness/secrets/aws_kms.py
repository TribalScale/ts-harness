"""AWS KMS encrypt/decrypt backend (preserves existing OAuth ciphertext format)."""

from __future__ import annotations

import base64
import os

from ts_harness.secrets.kms import FernetKmsBackend, looks_like_fernet


class AwsKmsBackend:
    """Encrypt/decrypt using AWS KMS; Fernet fallback when boto3/KMS is unavailable."""

    def __init__(self, key_id: str, *, region: str | None = None) -> None:
        self.key_id = key_id
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("CLOUD_REGION")
        # Fernet seed matches historical oauth_store behavior (seed = kms key id).
        self._fernet = FernetKmsBackend(key_id)

    def _client(self):
        import boto3

        kwargs = {}
        if self.region:
            kwargs["region_name"] = self.region
        return boto3.client("kms", **kwargs)

    def encrypt(self, plaintext: str) -> str:
        try:
            client = self._client()
            blob = client.encrypt(KeyId=self.key_id, Plaintext=plaintext.encode())["CiphertextBlob"]
            return base64.b64encode(blob).decode()
        except Exception:
            return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        try:
            if looks_like_fernet(ciphertext):
                return self._fernet.decrypt(ciphertext)
            raw = base64.b64decode(ciphertext)
            client = self._client()
            return client.decrypt(CiphertextBlob=raw)["Plaintext"].decode()
        except Exception:
            return self._fernet.decrypt(ciphertext)
