# Local OIDC dev issuer

A small OIDC provider for development. It stands in for Auth0 until the Auth0
tenant exists, and lets you sign in to the admin console as any user with any
`ts-harness-*` groups. Tokens have the same shape the Auth0 post-login Action
produces: `https://ts-harness/roles` and `https://ts-harness/groups` claims, a
verified email, RS256 signatures and a 1-hour lifetime.

Do not expose it outside a developer machine: anyone who can reach it can sign
in as anyone.

## Start it

```bash
ENTERPRISE_ASSETS_DIR=$PWD docker compose -f docker/docker-compose.oidc-dev.yml up -d
curl -s http://localhost:9400/.well-known/openid-configuration
```

Or without Docker, using the repo virtualenv:

```bash
.venv/bin/python scripts/dev/oidc-dev-issuer.py --issuer http://localhost:9400/ --port 9400
```

The signing key lives in the `oidc-dev-keys` volume, so tokens stay valid
across restarts. Pass `--key-file` to the script for the same effect.

## Point the runtime at it

```yaml
identity:
  provider: oidc
  preset: auth0
  issuer: http://localhost:9400/
  audience: https://ts-harness/api
  client_id: ts-harness-admin
  jwks_url: http://localhost:9400/.well-known/jwks.json
```

The runtime container reaches the issuer through the host, so when the runtime
runs in Docker, use an issuer URL both the browser and the container can
resolve (for example `http://host.docker.internal:9400/` on Docker Desktop, set
via `OIDC_DEV_ISSUER` when starting the compose file).

## Sign in

1. Open the admin console. The login page shows **Sign in with SSO**.
2. The issuer's form asks for a subject, email, name and groups. Use
   `ts-harness-users` plus team groups such as `ts-harness-team-payments` or
   `ts-harness-team-payments-approvers`; add `ts-harness-admins` for an admin.
3. Submit. The console exchanges the code with PKCE and opens a session.

Sign in as two different users in two browsers to try sharing and private
programs.
