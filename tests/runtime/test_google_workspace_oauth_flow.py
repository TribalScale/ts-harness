"""OAuth flow unit tests."""

from __future__ import annotations



from ts_harness.connectors.google_workspace.auth import oauth_flow


def test_build_and_parse_state():
    state = oauth_flow.build_state(tenant_id="default", email="user@example.com")
    parsed = oauth_flow.parse_state(state)
    assert parsed["tenant_id"] == "default"
    assert parsed["email"] == "user@example.com"


def test_build_consent_url(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_OAUTH_CALLBACK_URL", "https://example.com/oauth/google/callback")
    url = oauth_flow.build_consent_url(tenant_id="default", email="user@example.com")
    assert "accounts.google.com" in url
    assert "client_id=cid" in url
