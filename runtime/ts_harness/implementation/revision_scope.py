"""Limit a code-review revision to the files its blocking findings named."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from ts_harness.workflows.code_review import PIPELINE_WRITTEN, is_blocking

_PYTHON_SOURCES = ("requirements*.txt", "pyproject.toml")
_PYTHON_LOCKS = ("poetry.lock", "uv.lock", "Pipfile.lock")
_NODE_LOCKS = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")
_TEST_PATHS = (
    "test/*",
    "tests/*",
    "*/test/*",
    "*/tests/*",
    "*/__tests__/*",
    "__tests__/*",
    "features/*",
    "*/features/*",
    "*.feature",
    "*.test.*",
    "*.spec.*",
    "*test_*.py",
    "*_test.py",
    "*_test.go",
)


def revision_files_from_findings(findings: list[dict]) -> list[str]:
    files = {
        str(finding.get("file")).strip()
        for finding in findings
        if isinstance(finding, dict) and is_blocking(finding) and str(finding.get("file") or "").strip()
    }
    return sorted(files)


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _companions(path: str) -> tuple[str, ...]:
    name = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(name, pattern) for pattern in _PYTHON_SOURCES):
        return _PYTHON_LOCKS
    if name == "package.json":
        return _NODE_LOCKS
    return ()


def allowed_revision_paths(cited: list[str], repo_dir: Path) -> set[str]:
    allowed = {path.strip() for path in cited if path and path.strip()}
    for path in list(allowed):
        directory = _parent(path)
        for name in _companions(path):
            companion = _join(directory, name)
            if (repo_dir / companion).is_file():
                allowed.add(companion)
    return allowed


def _pipeline_written(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in PIPELINE_WRITTEN)


def is_test_path(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in _TEST_PATHS)


def unexpected_revision_paths(changed: list[str], allowed: set[str]) -> list[str]:
    if not allowed:
        return []
    return sorted(
        path
        for path in changed
        if path and path not in allowed and not _pipeline_written(path) and not is_test_path(path)
    )
