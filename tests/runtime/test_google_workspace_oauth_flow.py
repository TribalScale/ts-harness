"""OAuth flow unit tests."""

from __future__ import annotations



from ts_harness.connectors.google_workspace.auth import oauth_flow


def test_build_and_parse_state(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    state = oauth_flow.build_state(tenant_id="default", email="user@example.com")
    parsed = oauth_flow.parse_state(state)
    assert parsed["tenant_id"] == "default"
    assert parsed["email"] == "user@example.com"


def test_forged_or_altered_state_is_rejected(monkeypatch):
    import base64
    import json

    import pytest

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    forged = base64.urlsafe_b64encode(json.dumps({"tenant_id": "other", "email": "victim@example.com"}).encode()).decode()
    with pytest.raises(ValueError):
        oauth_flow.parse_state(forged)
    body, _, signature = oauth_flow.build_state(tenant_id="default", email="user@example.com").rpartition(".")
    with pytest.raises(ValueError):
        oauth_flow.parse_state(f"{forged}.{signature}")


def test_build_consent_url(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CALLBACK_URL", "https://example.com/oauth/google/callback")
    url = oauth_flow.build_consent_url(tenant_id="default", email="user@example.com")
    assert "accounts.google.com" in url
    assert "client_id=cid" in url
