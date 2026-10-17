"""OAuth credential encryption round-trip."""

from __future__ import annotations


from ts_harness.connectors.google_workspace.auth import oauth_store


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("TS_HARNESS_OAUTH_DEV_SECRET", "test-secret")
    plain = "super-secret-token"
    enc = oauth_store.encrypt_token(plain)
    assert oauth_store.decrypt_token(enc) == plain
