"""Helpers for reading implementation config at tool runtime."""

from __future__ import annotations

import os
from pathlib import Path

from ts_harness.config.schema import (
    ImplementationConfig,
    ImplementationRepoConfig,
    OpenCodeBackendConfig,
    RuntimeConfig,
    load_runtime_config,
)


def load_active_runtime_config() -> RuntimeConfig | None:
    path = os.environ.get("TS_HARNESS_CONFIG") or os.environ.get("TS_HARNESS_RUNTIME_CONFIG")
    if not path:
        config_dir = os.environ.get("TS_HARNESS_CONFIG_DIR", "").strip()
        if config_dir:
            path = str(Path(config_dir) / "ts-harness-runtime.yaml")
    if not path or not Path(path).is_file():
        return None
    return load_runtime_config(path)


def implementation_config(runtime: RuntimeConfig | None = None) -> ImplementationConfig:
    cfg = runtime or load_active_runtime_config()
    if cfg is None:
        return ImplementationConfig()
    return cfg.implementation


def repo_for_workflow(
    workflow_type: str = "implement_work_orders",
    *,
    runtime: RuntimeConfig | None = None,
) -> ImplementationRepoConfig:
    impl = implementation_config(runtime)
    return impl.repos.get(workflow_type) or ImplementationRepoConfig()


def opencode_backend_config(*, runtime: RuntimeConfig | None = None) -> OpenCodeBackendConfig:
    return implementation_config(runtime).backends.opencode
