"""GCP Cloud KMS encrypt/decrypt backend (optional google-cloud-kms dependency)."""

from __future__ import annotations

import base64

from ts_harness.secrets.kms import FernetKmsBackend, looks_like_fernet


class GcpKmsBackend:
    """Encrypt/decrypt using Cloud KMS; Fernet fallback when the library/API is unavailable."""

    def __init__(self, key_id: str) -> None:
        # Full resource name, e.g. projects/P/locations/L/keyRings/R/cryptoKeys/K
        self.key_id = key_id
        self._fernet = FernetKmsBackend(key_id)

    def _client(self):
        try:
            from google.cloud import kms  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "google-cloud-kms is required for GCP credentials encryption "
                "(pip install google-cloud-kms) or unset CREDENTIALS_ENCRYPTION_KEY_ID "
                "to use local Fernet"
            ) from exc
        return kms.KeyManagementServiceClient()

    def encrypt(self, plaintext: str) -> str:
        try:
            client = self._client()
            response = client.encrypt(
                request={"name": self.key_id, "plaintext": plaintext.encode("utf-8")}
            )
            return base64.b64encode(response.ciphertext).decode("ascii")
        except Exception:
            return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: str) -> str:
        try:
            if looks_like_fernet(ciphertext):
                return self._fernet.decrypt(ciphertext)
            raw = base64.b64decode(ciphertext)
            client = self._client()
            response = client.decrypt(request={"name": self.key_id, "ciphertext": raw})
            return response.plaintext.decode("utf-8")
        except Exception:
            return self._fernet.decrypt(ciphertext)
