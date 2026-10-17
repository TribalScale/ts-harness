"""Runtime config reload with last-known-good fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import copy
import os

import yaml

from ts_harness.config.schema import RuntimeConfig, load_runtime_config, parse_runtime_config


def _is_yaml_path(path: Path) -> bool:
    return path.suffix in {".yaml", ".yml"} or path.name.endswith((".yaml.tmpl", ".yml.tmpl"))


def default_lkg_path(config_path: str | Path) -> Path:
    path = Path(config_path)
    env_override = os.environ.get("TS_HARNESS_CONFIG_LKG")
    if env_override:
        return Path(env_override)
    if _is_yaml_path(path):
        return path.with_suffix(path.suffix + ".lkg")
    return path.with_suffix(path.suffix + ".lkg")


def save_last_known_good(config: RuntimeConfig, path: Path) -> None:
    if not config._last_known_good:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config._last_known_good, sort_keys=True), encoding="utf-8")


def load_last_known_good(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def reload_runtime_config(
    path: str | Path,
    *,
    lkg_path: str | Path | None = None,
) -> RuntimeConfig:
    path = Path(path)
    lkg_file = Path(lkg_path) if lkg_path else default_lkg_path(path)
    try:
        config = load_runtime_config(path)
        save_last_known_good(config, lkg_file)
        return config
    except Exception:
        cached = load_last_known_good(lkg_file)
        if cached is None:
            raise
        config = parse_runtime_config(copy.deepcopy(cached))
        config._last_known_good = copy.deepcopy(cached)
        return config
