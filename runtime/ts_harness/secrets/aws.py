"""AWS Secrets Manager provider for SecretResolver."""

from __future__ import annotations

import json
import os

def build_aws_secret_provider(*, region: str | None = None):
    region = region or os.environ.get("AWS_REGION", "us-east-1")

    def provider(secret_id: str) -> str:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for AWS secret resolution") from exc
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_id)
        raw = response.get("SecretString") or ""
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                for key in ("value", "password", "token", "secret"):
                    if key in payload:
                        return str(payload[key])
        except json.JSONDecodeError:
            pass
        return raw

    return provider


def default_secret_resolver():
    from ts_harness.secrets.resolver import SecretResolver

    provider = None
    if os.environ.get("TS_HARNESS_AWS_SECRETS", "").lower() == "true":
        provider = build_aws_secret_provider()
    return SecretResolver(provider=provider)
