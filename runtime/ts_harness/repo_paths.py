"""Resolve repository/asset root in dev checkouts and installed Docker images."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


def _has_repo_markers(path: Path) -> bool:
    # Require pyproject.toml so a bare /config/workflows mount (workflow-runner)
    # does not make repo_root() resolve to filesystem root and break scripts_dir().
    return (path / "pyproject.toml").exists() and (
        (path / "config" / "workflows").is_dir() or (path / "config").is_dir()
    )


@lru_cache(maxsize=1)
def repo_root() -> Path:
    env = os.environ.get("TS_HARNESS_ROOT", "").strip()
    if env:
        return Path(env).resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if _has_repo_markers(parent):
            return parent

    for candidate in (Path("/app"), Path.cwd()):
        if _has_repo_markers(candidate):
            return candidate.resolve()

    return Path("/app").resolve()


def scripts_dir() -> Path:
    return repo_root() / "scripts"


def config_dir() -> Path:
    return repo_root() / "config"


def workflows_dir() -> Path:
    override = os.environ.get("WORKFLOW_DEFINITIONS_DIR", "").strip()
    if override:
        return Path(override)
    return config_dir() / "workflows"


def programs_dir() -> Path:
    override = os.environ.get("PROGRAM_DEFINITIONS_DIR", "").strip()
    if override:
        return Path(override)
    return config_dir() / "programs"


def ensure_scripts_importable() -> None:
    scripts = str(scripts_dir())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
