"""In-process RSA key and JWT factory for OIDC tests."""

from __future__ import annotations

import base64
import time
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://login.example.com/"
AUDIENCE = "https://ts-harness/api"
CLIENT_ID = "spa-client"
KID = "fixture-key"


@lru_cache(maxsize=1)
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk() -> dict[str, Any]:
    numbers = private_key().public_key().public_numbers()

    def b64(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": b64(numbers.n), "e": b64(numbers.e)}


def make_token(**claims: Any) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "auth0|alice", "iat": now, "exp": now + 600}
    payload.update(claims)
    return jwt.encode(payload, private_key(), algorithm="RS256", headers={"kid": KID})


def provider_with_keys(config: Any) -> Any:
    from ts_harness.gateway.identity import OidcIdentityProvider

    provider = OidcIdentityProvider(config)
    provider._jwks_keys = [jwk()]
    provider._jwks_fetched_at = time.monotonic()
    return provider
