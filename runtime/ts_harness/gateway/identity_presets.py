"""Per-provider defaults for the parts of OIDC that providers leave open.

Explicit ``identity.*`` config always wins over a preset. With no preset the
runtime keeps its original behaviour, including Auth0-style endpoint guesses.
"""

from __future__ import annotations

from typing import Any

PRESETS: dict[str, dict[str, str]] = {
    "auth0": {
        "token_type": "access",
        "groups_claim": "https://ts-harness/groups",
        "roles_claim": "https://ts-harness/roles",
        "groups_source": "token",
        "directory": "auth0_management",
    },
    "entra": {
        "token_type": "access",
        "groups_claim": "groups",
        "roles_claim": "roles",
        "groups_source": "token",
        "directory": "scim",
    },
    "google": {
        "token_type": "id",
        "groups_claim": "groups",
        "roles_claim": "roles",
        "groups_source": "google_directory",
        "directory": "google_directory",
    },
    "okta": {
        "token_type": "access",
        "groups_claim": "groups",
        "roles_claim": "roles",
        "groups_source": "token",
        "directory": "scim",
    },
    "generic": {
        "token_type": "access",
        "groups_claim": "groups",
        "roles_claim": "roles",
        "groups_source": "token",
        "directory": "seen_users",
    },
}

LEGACY_DEFAULTS: dict[str, str] = {
    "token_type": "access",
    "groups_claim": "groups",
    "roles_claim": "roles",
    "groups_source": "token",
    "directory": "seen_users",
}

PRESET_SETTINGS = tuple(LEGACY_DEFAULTS)


def identity_setting(config: Any, name: str) -> str:
    """Return the explicit setting, else the preset default, else the legacy default."""
    if name not in LEGACY_DEFAULTS:
        raise KeyError(f"not a preset setting: {name}")
    explicit = getattr(config, name, None)
    if explicit:
        return str(explicit)
    preset = PRESETS.get(str(getattr(config, "preset", None) or ""))
    if preset:
        return preset[name]
    return LEGACY_DEFAULTS[name]


def uses_legacy_endpoints(config: Any) -> bool:
    """No preset or the Auth0 preset: ``{issuer}/authorize`` and ``/oauth/token`` are valid guesses."""
    return str(getattr(config, "preset", None) or "") in {"", "auth0"}
