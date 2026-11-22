"""Limit a code-review revision to the files its blocking findings named."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from ts_harness.workflows.code_review import PIPELINE_WRITTEN, is_blocking, is_test_path

_PYTHON_SOURCES = ("requirements*.txt", "pyproject.toml")
_PYTHON_LOCKS = ("poetry.lock", "uv.lock", "Pipfile.lock")
_NODE_LOCKS = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")


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


# Module glue next to a cited file: a fix often has to export a new function or widen a type.
_GLUE = re.compile(r"^(?:index|types|constants|schemas?)\.[\w.]+$|\.types\.[jt]sx?$|\.d\.ts$")
_IMPORT = re.compile(
    r"""(?:^|\n)\s*(?:import\s[^'"]*?from\s*|import\s*|export\s[^'"]*?from\s*|(?:const|let|var)\s+\S+\s*=\s*require\()['"](\.{1,2}/[^'"]+)['"]"""
)
_PY_IMPORT = re.compile(r"(?:^|\n)\s*from\s+(\.+[\w.]*)\s+import\s")
_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")


def _resolve_relative(repo_dir: Path, base: str, spec: str) -> str | None:
    """``./types`` from ``src/a/b.ts`` -> ``src/a/types.ts`` when such a file exists."""
    target = (Path(_parent(base)) / spec).as_posix()
    parts: list[str] = []
    for part in target.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    target = "/".join(parts)
    candidates = [target] + [target + suffix for suffix in _SOURCE_SUFFIXES]
    candidates += [f"{target}/index{suffix}" for suffix in _SOURCE_SUFFIXES]
    for candidate in candidates:
        if (repo_dir / candidate).is_file():
            return candidate
    return None


def _direct_imports(repo_dir: Path, path: str) -> set[str]:
    """Files ``path`` imports with a relative import (JS/TS and Python)."""
    try:
        text = (repo_dir / path).read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return set()
    found: set[str] = set()
    if path.endswith(_SOURCE_SUFFIXES):
        for spec in _IMPORT.findall(text):
            resolved = _resolve_relative(repo_dir, path, spec.split("?")[0])
            if resolved:
                found.add(resolved)
    elif path.endswith(".py"):
        for spec in _PY_IMPORT.findall(text):
            dots = len(spec) - len(spec.lstrip("."))
            module = spec.lstrip(".").replace(".", "/")
            base = Path(_parent(path))
            for _ in range(dots - 1):
                base = base.parent
            candidate = (base / f"{module}.py").as_posix() if module else ""
            if candidate and (repo_dir / candidate).is_file():
                found.add(candidate)
    return found


def allowed_revision_paths(cited: list[str], repo_dir: Path) -> set[str]:
    """The cited files, their manifests' lockfiles, the module glue beside them (``index``,
    ``types``, ``constants``, ``schema`` files) and the files they import directly."""
    allowed = {path.strip() for path in cited if path and path.strip()}
    for path in list(allowed):
        directory = _parent(path)
        for name in _companions(path):
            companion = _join(directory, name)
            if (repo_dir / companion).is_file():
                allowed.add(companion)
        folder = repo_dir / directory if directory else repo_dir
        if folder.is_dir():
            for sibling in folder.iterdir():
                if sibling.is_file() and _GLUE.search(sibling.name):
                    allowed.add(_join(directory, sibling.name))
        allowed |= _direct_imports(repo_dir, path)
    return allowed


def _pipeline_written(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in PIPELINE_WRITTEN)


def unexpected_revision_paths(changed: list[str], allowed: set[str]) -> list[str]:
    if not allowed:
        return []
    return sorted(
        path
        for path in changed
        if path and path not in allowed and not _pipeline_written(path) and not is_test_path(path)
    )
