"""JavaScript monorepos: which workspace package a changed file belongs to, and how to run its tools.

npm, yarn and pnpm workspaces (``package.json`` ``workspaces`` or ``pnpm-workspace.yaml``),
with or without Turborepo or Nx on top. Checks that a single-package repo runs at the root
(lint, type check, build, Playwright) run per changed package here, from the package
directory, because that is where its ESLint config, tsconfig and Playwright config live.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

_SKIP = {"node_modules", ".git", "dist", "build", ".next", ".turbo"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def workspace_globs(repo_dir: Path) -> list[str]:
    repo_dir = Path(repo_dir)
    globs: list[str] = []
    pnpm = repo_dir / "pnpm-workspace.yaml"
    if pnpm.is_file():
        import yaml

        try:
            data = yaml.safe_load(pnpm.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            data = {}
        globs += [str(g) for g in (data.get("packages") or []) if isinstance(g, str)]
    workspaces = _read_json(repo_dir / "package.json").get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    if isinstance(workspaces, list):
        globs += [str(g) for g in workspaces if isinstance(g, str)]
    return [g for g in globs if not g.startswith("!")]


def workspace_packages(repo_dir: Path) -> list[Path]:
    """Package directories (with a package.json) the workspace globs match."""
    repo_dir = Path(repo_dir)
    found: set[Path] = set()
    for pattern in workspace_globs(repo_dir):
        pattern = pattern.rstrip("/")
        for path in repo_dir.glob(pattern):
            if path.is_dir() and (path / "package.json").is_file() and not set(path.relative_to(repo_dir).parts) & _SKIP:
                found.add(path)
    return sorted(found)


def is_monorepo(repo_dir: Path) -> bool:
    return bool(workspace_packages(repo_dir))


def owning_package(repo_dir: Path, rel_path: str) -> Path | None:
    """The workspace package containing ``rel_path``, or ``None`` for root files."""
    target = (Path(repo_dir) / rel_path).resolve()
    best: Path | None = None
    for package in workspace_packages(repo_dir):
        try:
            target.relative_to(package.resolve())
        except ValueError:
            continue
        if best is None or len(package.parts) > len(best.parts):
            best = package
    return best


def changed_packages(repo_dir: Path, changed_files: list[str]) -> list[Path]:
    packages = {owning_package(repo_dir, path) for path in changed_files}
    return sorted(p for p in packages if p is not None)


def package_name(package_dir: Path) -> str:
    return str(_read_json(Path(package_dir) / "package.json").get("name") or Path(package_dir).name)


def package_scripts(package_dir: Path) -> dict[str, str]:
    scripts = _read_json(Path(package_dir) / "package.json").get("scripts")
    return {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}


def find_bin(repo_dir: Path, start: Path, name: str) -> Path | None:
    """``node_modules/.bin/<name>`` nearest to ``start``, up to the repo root (hoisted installs)."""
    root = Path(repo_dir).resolve()
    current = Path(start)
    while True:
        candidate = current / "node_modules" / ".bin" / name
        if candidate.exists():
            return candidate
        if current.resolve() == root or current.parent == current:
            return None
        current = current.parent


def in_dir(directory: Path, command: list[str]) -> list[str]:
    """``command`` run from ``directory`` (GNU ``env -C``), as one argv for host verification."""
    return ["env", "-C", str(directory), *command]


def workspace_run(repo_dir: Path, manager: str, package_dir: Path, script: str) -> list[str]:
    """Run one package's script through the workspace tool, so its dependencies resolve."""
    name = package_name(package_dir)
    turbo = find_bin(repo_dir, repo_dir, "turbo")
    if turbo is not None and script == "build":
        # Turborepo builds the package's workspace dependencies first (^build).
        return [str(turbo), "run", "build", f"--filter={name}", "--output-logs=errors-only"]
    if manager == "pnpm":
        return ["pnpm", "--filter", name, "run", script]
    if manager == "yarn":
        return ["yarn", "workspace", name, "run", script]
    return ["npm", "run", script, "-w", name]


def describe(command: list[str]) -> str:
    return shlex.join(command)
