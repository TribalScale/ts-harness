#!/usr/bin/env python3
"""Local OIDC issuer for development, standing in for Auth0.

Serves discovery, JWKS, an ``/authorize`` form to pick a user and groups, and a
PKCE ``/token`` endpoint. Tokens carry ``https://ts-harness/roles`` and
``https://ts-harness/groups`` claims, the same shape the Auth0 post-login
Action produces. Never expose this server outside a developer machine.

    python3 scripts/dev/oidc-dev-issuer.py --issuer http://localhost:9400/ --port 9400
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROLES_CLAIM = "https://ts-harness/roles"
GROUPS_CLAIM = "https://ts-harness/groups"
CODE_TTL = 300
TOKEN_TTL = 3600
KID = "dev-issuer-1"


def _b64url_uint(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_or_create_key(path: Path | None) -> rsa.RSAPrivateKey:
    if path and path.is_file():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)  # type: ignore[return-value]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        path.chmod(0o600)
    return key


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class DevIssuer:
    def __init__(self, issuer: str, key: rsa.RSAPrivateKey):
        self.issuer = issuer if issuer.endswith("/") else issuer + "/"
        self.key = key
        self.codes: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def url(self, path: str) -> str:
        return self.issuer + path.lstrip("/")

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.url("authorize"),
            "token_endpoint": self.url("oauth/token"),
            "jwks_uri": self.url(".well-known/jwks.json"),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "subject_types_supported": ["public"],
        }

    def jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": KID,
                    "n": _b64url_uint(numbers.n),
                    "e": _b64url_uint(numbers.e),
                }
            ]
        }

    def issue_code(self, request: dict[str, str], user: dict[str, Any]) -> str:
        code = secrets.token_urlsafe(24)
        with self.lock:
            self.codes[code] = {**request, "user": user, "expires": time.time() + CODE_TTL}
        return code

    def exchange(self, form: dict[str, str]) -> dict[str, Any]:
        if form.get("grant_type") != "authorization_code":
            raise ValueError("unsupported_grant_type")
        with self.lock:
            entry = self.codes.pop(form.get("code", ""), None)
        if not entry or entry["expires"] < time.time():
            raise ValueError("invalid_grant")
        if form.get("client_id") != entry["client_id"] or form.get("redirect_uri") != entry["redirect_uri"]:
            raise ValueError("invalid_grant")
        if pkce_s256(form.get("code_verifier", "")) != entry["code_challenge"]:
            raise ValueError("invalid_grant")
        user = entry["user"]
        now = int(time.time())
        base = {
            "iss": self.issuer,
            "sub": user["sub"],
            "iat": now,
            "exp": now + TOKEN_TTL,
            "email": user["email"],
            "email_verified": True,
            "name": user.get("name") or user["email"],
            ROLES_CLAIM: user["groups"],
            GROUPS_CLAIM: user["groups"],
        }
        headers = {"kid": KID}
        access = jwt.encode(
            {**base, "aud": entry.get("audience") or entry["client_id"], "scope": entry.get("scope", "")},
            self.key,
            algorithm="RS256",
            headers=headers,
        )
        id_token = jwt.encode({**base, "aud": entry["client_id"]}, self.key, algorithm="RS256", headers=headers)
        return {
            "access_token": access,
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
        }


AUTHORIZE_FORM = """<!doctype html>
<html><head><title>TS Harness dev sign-in</title>
<style>body{{font-family:sans-serif;max-width:32rem;margin:3rem auto}}label{{display:block;margin:.75rem 0}}
input{{width:100%;padding:.4rem}}</style></head>
<body><h1>Dev sign-in</h1>
<p>Local OIDC issuer for development. Pick who to sign in as.</p>
<form method="post" action="authorize">
{hidden}
<label>Subject <input name="sub" value="dev|alice"></label>
<label>Email <input name="email" value="alice@example.com"></label>
<label>Name <input name="name" value="Alice Example"></label>
<label>Groups, comma separated
<input name="groups" value="ts-harness-users,ts-harness-team-payments"></label>
<button type="submit">Sign in</button>
</form></body></html>
"""

FORWARDED_PARAMS = ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "audience", "scope")


def make_handler(issuer: DevIssuer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            pass

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path.endswith("/.well-known/openid-configuration"):
                self._json(200, issuer.discovery())
            elif path.endswith("/.well-known/jwks.json"):
                self._json(200, issuer.jwks())
            elif path.endswith("/authorize"):
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if query.get("response_type") != "code" or query.get("code_challenge_method") != "S256":
                    self._json(400, {"error": "unsupported_response_type"})
                    return
                hidden = "\n".join(
                    f'<input type="hidden" name="{name}" value="{html.escape(query.get(name, ""))}">'
                    for name in FORWARDED_PARAMS
                )
                body = AUTHORIZE_FORM.format(hidden=hidden).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/")
            form = self._form()
            if path.endswith("/authorize"):
                user = {
                    "sub": form.get("sub") or "dev|user",
                    "email": form.get("email") or "user@example.com",
                    "name": form.get("name") or "",
                    "groups": [g.strip() for g in (form.get("groups") or "").split(",") if g.strip()],
                }
                request = {name: form.get(name, "") for name in FORWARDED_PARAMS}
                code = issuer.issue_code(request, user)
                params = {"code": code}
                if request.get("state"):
                    params["state"] = request["state"]
                self.send_response(302)
                self.send_header("Location", f"{request['redirect_uri']}?{urlencode(params)}")
                self.end_headers()
            elif path.endswith("/oauth/token"):
                try:
                    self._json(200, issuer.exchange(form))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            else:
                self._json(404, {"error": "not_found"})

    return Handler


def serve(issuer_url: str, port: int, key_file: Path | None = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    issuer = DevIssuer(issuer_url, load_or_create_key(key_file))
    server = ThreadingHTTPServer((host, port), make_handler(issuer))
    server.issuer = issuer  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--issuer", default="http://localhost:9400/")
    parser.add_argument("--port", type=int, default=9400)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--key-file", type=Path, default=None, help="persist the signing key across restarts")
    args = parser.parse_args()
    server = serve(args.issuer, args.port, args.key_file, args.host)
    print(f"OIDC dev issuer at {args.issuer} (port {args.port})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
