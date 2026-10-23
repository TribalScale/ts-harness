"""Deployment profile: one EC2 host (``single_host``) or the distributed topology (``scale_out``).

The profile only picks defaults for topology settings. Each setting resolves in
this order: its environment variable, ``deployment.overrides`` in the runtime
config, then the profile default.
"""

from __future__ import annotations

import os
from typing import Any

SINGLE_HOST = "single_host"
SCALE_OUT = "scale_out"
PROFILES = (SINGLE_HOST, SCALE_OUT)

PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    SINGLE_HOST: {
        "coding_launcher": "local",
        "coding_max_running": 3,
        "lane_caps": "fast=10,agent=3,coding=3",
        "github_reconcile_interval_seconds": 60,
        "runtime_replicas": 1,
    },
    SCALE_OUT: {
        "coding_launcher": "ecs",
        "coding_max_running": 20,
        "lane_caps": "fast=20,agent=6,coding=20",
        "github_reconcile_interval_seconds": 900,
        "runtime_replicas": 2,
    },
}

SETTING_ENV = {
    "coding_launcher": "CODING_LAUNCHER",
    "coding_max_running": "CODING_MAX_RUNNING",
    "lane_caps": "WORKFLOW_RUNNER_LANE_CAPS",
    "github_reconcile_interval_seconds": "GITHUB_RECONCILE_INTERVAL_SECONDS",
    "runtime_replicas": "AGENT_RUNTIME_REPLICAS",
}


def normalize_profile(value: Any) -> str:
    """Blank or unrendered template values mean the default profile."""
    text = str(value or "").strip().lower()
    if not text or text.startswith("{{"):
        return SINGLE_HOST
    if text not in PROFILES:
        raise ValueError(f"deployment profile must be one of {', '.join(PROFILES)}, got {value!r}")
    return text


def _runtime_deployment(runtime: Any | None) -> Any | None:
    if runtime is None:
        try:
            from ts_harness.implementation.config import load_active_runtime_config

            runtime = load_active_runtime_config()
        except Exception:
            return None
    return getattr(runtime, "deployment", None)


def current_profile(runtime: Any | None = None) -> str:
    env = os.environ.get("DEPLOYMENT_PROFILE", "")
    if env.strip() and not env.strip().startswith("{{"):
        return normalize_profile(env)
    deployment = _runtime_deployment(runtime)
    return normalize_profile(getattr(deployment, "profile", None))


def resolve_setting(name: str, runtime: Any | None = None) -> Any:
    """Return a topology setting, coerced to the type of its profile default."""
    if name not in SETTING_ENV:
        raise KeyError(f"unknown deployment setting: {name}")
    default = PROFILE_DEFAULTS[current_profile(runtime)][name]
    raw: Any = os.environ.get(SETTING_ENV[name], "").strip()
    if raw == "":
        deployment = _runtime_deployment(runtime)
        overrides = getattr(deployment, "overrides", None) or {}
        raw = overrides.get(name, "")
    if raw in ("", None):
        return default
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return str(raw)


def parse_lane_caps(text: str) -> dict[str, int]:
    """``fast=10,agent=3`` to ``{"fast": 10, "agent": 3}``; bad entries are skipped."""
    caps: dict[str, int] = {}
    for part in str(text or "").split(","):
        name, _, value = part.partition("=")
        name = name.strip()
        try:
            count = int(value)
        except ValueError:
            continue
        if name and count > 0:
            caps[name] = count
    return caps
