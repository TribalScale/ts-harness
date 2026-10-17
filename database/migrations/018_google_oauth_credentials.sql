BEGIN;

-- Per-user Google OAuth tokens (encrypted at rest).
CREATE TABLE IF NOT EXISTS google_oauth_credentials (
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL,
  enc_access_token TEXT NOT NULL,
  enc_refresh_token TEXT NOT NULL,
  google_email TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, email)
);

CREATE INDEX IF NOT EXISTS idx_google_oauth_credentials_expires
  ON google_oauth_credentials (tenant_id, expires_at);

COMMIT;
