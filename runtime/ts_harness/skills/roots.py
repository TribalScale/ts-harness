"""Skill roots: read-only builtin skills plus a writable managed directory."""

from __future__ import annotations

import os
from pathlib import Path


def builtin_root() -> Path:
    return Path(os.environ.get("SKILLS_DIR", "skills"))


def managed_root() -> Path:
    managed = str(os.environ.get("SKILLS_MANAGED_DIR") or "").strip()
    return Path(managed) if managed else builtin_root()


def _same_dir(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def catalog_roots() -> list[Path]:
    builtin = builtin_root()
    managed = managed_root()
    if _same_dir(builtin, managed):
        return [builtin]
    return [builtin, managed]


def is_builtin_path(path: str | Path) -> bool:
    """True when ``path`` is a repo skill and the managed root is a separate directory."""
    builtin = builtin_root()
    if _same_dir(builtin, managed_root()):
        return False
    try:
        return Path(path).resolve().is_relative_to(builtin.resolve())
    except OSError:
        return False
