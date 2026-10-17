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


def resolve_coding_backend_credentials(
    *,
    provider: str | None = None,
    auth_profiles_path: Path | None = None,
) -> tuple[str, str]:
    """Return ``(provider, api_key)`` from env first, then auth-profiles."""
    profiles = load_auth_profiles(auth_profiles_path)
    resolved_provider = (
        (provider or "").strip()
        or os.environ.get("CODING_BACKEND_PROVIDER", "").strip()
        or str(profiles.get("CODING_BACKEND_PROVIDER") or "").strip()
    )
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
