"""Provider-selectable KMS backends for OAuth credential encryption."""

from __future__ import annotations

import base64
from unittest import mock


from ts_harness.connectors.google_workspace.auth import oauth_store
from ts_harness.secrets import aws_kms as aws_kms_mod
from ts_harness.secrets import gcp_kms as gcp_kms_mod
from ts_harness.secrets.kms import (
    FernetKmsBackend,
    get_kms_backend,
    resolve_cloud_provider,
    resolve_credentials_encryption_key_id,
)


def test_resolve_cloud_provider_prefers_enterprise_env(monkeypatch):
    monkeypatch.delenv("CLOUD_PROVIDER", raising=False)
    monkeypatch.setenv("TS_HARNESS_CLOUD_PROVIDER", "gcp")
    assert resolve_cloud_provider() == "gcp"


def test_resolve_key_id_aliases(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_ENCRYPTION_KEY_ID", raising=False)
    monkeypatch.setenv("AGENT_CREDENTIALS_KMS_KEY_ID", "alias/legacy")
    assert resolve_credentials_encryption_key_id() == "alias/legacy"
    monkeypatch.setenv("CREDENTIALS_ENCRYPTION_KEY_ID", "projects/p/locations/global/keyRings/r/cryptoKeys/k")
    assert resolve_credentials_encryption_key_id().startswith("projects/")


def test_fernet_backend_roundtrip(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_ENCRYPTION_KEY_ID", raising=False)
    monkeypatch.delenv("AGENT_CREDENTIALS_KMS_KEY_ID", raising=False)
    monkeypatch.setenv("TS_HARNESS_OAUTH_DEV_SECRET", "test-secret")
    backend = get_kms_backend()
    assert isinstance(backend, FernetKmsBackend)
    assert backend.decrypt(backend.encrypt("token-a")) == "token-a"


def test_aws_backend_preserves_ciphertext_format(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("CREDENTIALS_ENCRYPTION_KEY_ID", "alias/ts-harness-agent-credentials")

    class _Client:
        def encrypt(self, KeyId, Plaintext):  # noqa: N803
            assert KeyId.startswith("alias/")
            return {"CiphertextBlob": b"\x01aws-blob:" + Plaintext}

        def decrypt(self, CiphertextBlob):  # noqa: N803
            assert CiphertextBlob.startswith(b"\x01aws-blob:")
            return {"Plaintext": CiphertextBlob[len(b"\x01aws-blob:") :]}

    with mock.patch.object(aws_kms_mod.AwsKmsBackend, "_client", return_value=_Client()):
        backend = get_kms_backend()
        enc = backend.encrypt("oauth-token")
        # Historical format: raw base64 of CiphertextBlob (not Fernet gAAAA...).
        assert not enc.startswith("gAAAA")
        raw = base64.b64decode(enc)
        assert raw.startswith(b"\x01aws-blob:")
        assert backend.decrypt(enc) == "oauth-token"


def test_aws_backend_falls_back_to_fernet_without_boto3(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("AGENT_CREDENTIALS_KMS_KEY_ID", "alias/ts-harness-agent-credentials")

    def _boom(self):
        raise ImportError("boto3 missing")

    with mock.patch.object(aws_kms_mod.AwsKmsBackend, "_client", _boom):
        backend = get_kms_backend()
        enc = backend.encrypt("plain")
        assert enc.startswith("gAAAA")
        assert backend.decrypt(enc) == "plain"


def test_gcp_backend_uses_cloud_kms_when_available(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "gcp")
    key = "projects/p/locations/global/keyRings/r/cryptoKeys/k"
    monkeypatch.setenv("CREDENTIALS_ENCRYPTION_KEY_ID", key)

    class _Resp:
        def __init__(self, data: bytes):
            self.ciphertext = data
            self.plaintext = data

    class _Client:
        def encrypt(self, request):
            assert request["name"] == key
            return _Resp(b"gcp:" + request["plaintext"])

        def decrypt(self, request):
            assert request["name"] == key
            assert request["ciphertext"].startswith(b"gcp:")
            return _Resp(request["ciphertext"][len(b"gcp:") :])

    with mock.patch.object(gcp_kms_mod.GcpKmsBackend, "_client", return_value=_Client()):
        backend = get_kms_backend()
        enc = backend.encrypt("oauth-token")
        assert not enc.startswith("gAAAA")
        assert backend.decrypt(enc) == "oauth-token"


def test_gcp_backend_import_error_falls_back_to_fernet(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "gcp")
    monkeypatch.setenv(
        "CREDENTIALS_ENCRYPTION_KEY_ID",
        "projects/p/locations/global/keyRings/r/cryptoKeys/k",
    )

    def _boom(self):
        raise ImportError("google-cloud-kms missing")

    with mock.patch.object(gcp_kms_mod.GcpKmsBackend, "_client", _boom):
        backend = get_kms_backend()
        enc = backend.encrypt("plain")
        assert enc.startswith("gAAAA")
        assert backend.decrypt(enc) == "plain"


def test_oauth_store_uses_selected_backend(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.delenv("CREDENTIALS_ENCRYPTION_KEY_ID", raising=False)
    monkeypatch.delenv("AGENT_CREDENTIALS_KMS_KEY_ID", raising=False)
    monkeypatch.setenv("TS_HARNESS_OAUTH_DEV_SECRET", "oauth-dev")
    plain = "super-secret-token"
    enc = oauth_store.encrypt_token(plain)
    assert oauth_store.decrypt_token(enc) == plain


def test_get_kms_backend_selects_gcp_class(monkeypatch):
    monkeypatch.setenv("TS_HARNESS_CLOUD_PROVIDER", "gcp")
    monkeypatch.delenv("CLOUD_PROVIDER", raising=False)
    monkeypatch.setenv(
        "CREDENTIALS_ENCRYPTION_KEY_ID",
        "projects/p/locations/global/keyRings/r/cryptoKeys/k",
    )
    backend = get_kms_backend()
    assert isinstance(backend, gcp_kms_mod.GcpKmsBackend)
