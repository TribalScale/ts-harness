"""Encrypt/decrypt and persist Google OAuth tokens."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from ts_harness.secrets.kms import get_kms_backend, resolve_credentials_encryption_key_id


def _kms_key_id_from_config() -> str:
    """Prefer env (install/bootstrap); config kms_key_id is applied via env in compose."""
    return resolve_credentials_encryption_key_id()


def _backend():
    return get_kms_backend(key_id=_kms_key_id_from_config() or None)


def encrypt_token(plaintext: str) -> str:
    return _backend().encrypt(plaintext)


def decrypt_token(ciphertext: str) -> str:
    return _backend().decrypt(ciphertext)


def _db_url() -> str | None:
    return os.environ.get("TS_HARNESS_SESSION_DB_URL")


def _connect():
    import psycopg

    url = _db_url()
    if not url:
        raise RuntimeError("TS_HARNESS_SESSION_DB_URL required for Google OAuth storage")
    return psycopg.connect(url, connect_timeout=5)


def save_oauth_credentials(
    *,
    tenant_id: str,
    email: str,
    access_token: str,
    refresh_token: str,
    google_email: str | None,
    expires_in: int = 3600,
) -> None:
    enc_access = encrypt_token(access_token)
    enc_refresh = encrypt_token(refresh_token)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO google_oauth_credentials
              (tenant_id, email, enc_access_token, enc_refresh_token, google_email, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, email) DO UPDATE SET
              enc_access_token = EXCLUDED.enc_access_token,
              enc_refresh_token = EXCLUDED.enc_refresh_token,
              google_email = EXCLUDED.google_email,
              expires_at = EXCLUDED.expires_at,
              connected_at = now()
            """,
            (tenant_id, email.lower(), enc_access, enc_refresh, google_email, expires_at),
        )
        conn.commit()


def delete_oauth_credentials(*, tenant_id: str, email: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM google_oauth_credentials WHERE tenant_id = %s AND email = %s",
            (tenant_id, email.lower()),
        )
        conn.commit()


def get_oauth_credentials(*, tenant_id: str, email: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT enc_access_token, enc_refresh_token, google_email, expires_at
            FROM google_oauth_credentials
            WHERE tenant_id = %s AND email = %s
            """,
            (tenant_id, email.lower()),
        ).fetchone()
    if not row:
        return None
    enc_access, enc_refresh, google_email, expires_at = row
    return {
        "access_token": decrypt_token(enc_access),
        "refresh_token": decrypt_token(enc_refresh),
        "google_email": google_email,
        "expires_at": expires_at,
    }


def refresh_oauth_token(
    *,
    tenant_id: str,
    email: str,
    client_id: str,
    client_secret: str,
) -> str | None:
    creds = get_oauth_credentials(tenant_id=tenant_id, email=email)
    if not creds:
        return None
    expires_at = creds["expires_at"]
    if isinstance(expires_at, datetime):
        if expires_at.timetz() is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            return creds["access_token"]
    import httpx

    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": creds["refresh_token"],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    access = data["access_token"]
    save_oauth_credentials(
        tenant_id=tenant_id,
        email=email,
        access_token=access,
        refresh_token=creds["refresh_token"],
        google_email=creds.get("google_email"),
        expires_in=int(data.get("expires_in", 3600)),
    )
    return access
