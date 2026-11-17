"""Load coding-backend credentials for host subprocesses."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROVIDER_ENV_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}


def default_auth_profiles_path(agent_id: str = "dev-agent") -> Path:
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / ".ts-harness" / "agents" / agent_id / "agent" / "auth-profiles.json"
    return Path.home() / ".ts-harness" / "agents" / agent_id / "agent" / "auth-profiles.json"


def load_auth_profiles(path: Path | None = None) -> dict[str, Any]:
    auth_path = path or default_auth_profiles_path()
    if not auth_path.is_file():
        return {}
    try:
        data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def proxy_routing() -> bool:
    """OpenCode calls go through the LLM proxy when its URL and a token (or the secret to mint one) are set."""
    from ts_harness.llm_proxy.tokens import proxy_secret, proxy_url

    return bool(proxy_url() and (os.environ.get("LLM_PROXY_TOKEN", "").strip() or proxy_secret()))


def proxy_token(scope: dict[str, Any] | None = None) -> str:
    """The job's ``LLM_PROXY_TOKEN``, or one minted for ``scope`` where the secret is available."""
    from ts_harness.llm_proxy.tokens import mint_proxy_token, proxy_secret

    token = os.environ.get("LLM_PROXY_TOKEN", "").strip()
    if token:
        return token
    return mint_proxy_token(scope or {}) if proxy_secret() else ""


def resolve_coding_backend_credentials(
    *,
    provider: str | None = None,
    auth_profiles_path: Path | None = None,
    scope: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(provider, api_key)`` from env first, then auth-profiles.

    With the LLM proxy on, the key is a proxy token labelled with ``scope``.
    """
    profiles = load_auth_profiles(auth_profiles_path)
    resolved_provider = (
        (provider or "").strip()
        or os.environ.get("CODING_BACKEND_PROVIDER", "").strip()
        or str(profiles.get("CODING_BACKEND_PROVIDER") or "").strip()
    )
    if proxy_routing():
        return resolved_provider, proxy_token(scope)
    api_key = (
        os.environ.get("CODING_BACKEND_API_KEY", "").strip()
        or str(profiles.get("CODING_BACKEND_API_KEY") or "").strip()
    )
    return resolved_provider, api_key


def provider_child_env(provider: str, api_key: str) -> dict[str, str]:
    """Build env overrides for an OpenCode child process (isolates gateway keys)."""
    env: dict[str, str] = {
        "CODING_BACKEND_PROVIDER": provider,
        "CODING_BACKEND_API_KEY": api_key,
    }
    native = PROVIDER_ENV_MAP.get(provider.lower())
    if native and api_key:
        env[native] = api_key
    return env


def coding_child_env(provider: str, api_key: str, *, config_dir: Path) -> dict[str, str]:
    """Env for an OpenCode child. Through the proxy, a config file in ``config_dir`` points the
    provider at it and every native key slot holds the proxy token, so no real key reaches OpenCode."""
    if not proxy_routing():
        return provider_child_env(provider, api_key)
    from ts_harness.llm_proxy.tokens import proxy_url

    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "opencode-llm-proxy.json"
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider: {"options": {"baseURL": f"{proxy_url()}/{provider}/v1", "apiKey": "{env:LLM_PROXY_TOKEN}"}}
        },
    }
    config_path.write_text(json.dumps(config, indent=2))
    env = provider_child_env(provider, api_key)
    env.update({name: api_key for name in set(PROVIDER_ENV_MAP.values())})
    env.update({"LLM_PROXY_TOKEN": api_key, "OPENCODE_CONFIG": str(config_path)})
    return env


def coding_usage_scope(request: Any) -> dict[str, Any]:
    return {
        "program": getattr(request, "program_id", None),
        "workflow": getattr(request, "delivery_workflow_id", None),
        "user": getattr(request, "requested_by", None),
        "agent": "coding-backend",
    }
