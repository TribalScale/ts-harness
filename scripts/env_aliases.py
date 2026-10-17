"""Environment variable aliases for scripts that run without the ts_harness package."""

from __future__ import annotations

import os


def env_first(*names: str, default: str = "") -> str:
    """Return the first non-empty environment value for the given names."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return default
