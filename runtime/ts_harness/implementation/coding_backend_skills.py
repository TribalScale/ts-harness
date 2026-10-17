"""Install and materialize coding-backend skills for the active coding tool."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ts_harness.repo_paths import repo_root

# Relative project skill roots keyed by implementation.coding_backend.
_PROJECT_SKILL_ROOTS: dict[str, str] = {
    "opencode": ".opencode/skills",
}

# Kept for detection/stripping only — never install Superpowers into WO clones.
# Non-interactive OpenCode must not load brainstorming (waits for human approval).
SUPERPOWERS_OPENCODE_PLUGIN = "superpowers@git+https://github.com/obra/superpowers.git"


def coding_backend_skills_source(root: Path | None = None) -> Path:
    """Return ``skills/coding-backend`` under the ts-harness checkout/image."""
    base = Path(root) if root is not None else repo_root()
    return (base / "skills" / "coding-backend").resolve()


def project_skills_relpath(coding_backend: str) -> str:
    """Return the in-repo relative skills root for ``coding_backend``."""
    backend = str(coding_backend or "").strip().lower()
    if backend not in _PROJECT_SKILL_ROOTS:
        raise ValueError(f"unsupported coding_backend for skills materialize: {coding_backend!r}")
    return _PROJECT_SKILL_ROOTS[backend]


def list_coding_backend_skill_dirs(source_root: Path | None = None) -> list[Path]:
    """List skill directories that contain ``SKILL.md`` under the coding-backend source."""
    root = coding_backend_skills_source() if source_root is None else Path(source_root).resolve()
    if not root.is_dir():
        return []
    skills: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            skills.append(child)
    return skills


def install_coding_backend_skills(
    destination_root: Path,
    *,
    source_root: Path | None = None,
) -> list[str]:
    """Copy skill dirs into ``destination_root/<name>/``. Returns skill names installed."""
    dest = Path(destination_root)
    dest.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for skill_dir in list_coding_backend_skill_dirs(source_root):
        name = skill_dir.name
        target = dest / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill_dir, target)
        installed.append(name)
    return installed


def materialize_coding_backend_skills(
    repo_dir: Path,
    *,
    coding_backend: str = "opencode",
    source_root: Path | None = None,
) -> list[str]:
    """Copy coding-backend skills into the app clone for ``coding_backend``.

    Returns relative paths written (e.g. ``.opencode/skills/implement-work-order/SKILL.md``).
    """
    rel_root = project_skills_relpath(coding_backend)
    dest = Path(repo_dir) / rel_root
    resolved_repo = Path(repo_dir).resolve()
    resolved_dest = dest.resolve()
    if not str(resolved_dest).startswith(str(resolved_repo)):
        raise ValueError(f"skills destination escapes repo: {dest}")
    names = install_coding_backend_skills(dest, source_root=source_root)
    written: list[str] = []
    for name in names:
        skill_md = dest / name / "SKILL.md"
        if skill_md.is_file():
            written.append(f"{rel_root}/{name}/SKILL.md".replace("\\", "/"))
    return written


def _is_superpowers_plugin_spec(spec: object) -> bool:
    text = str(spec or "").strip().lower()
    return "superpowers" in text


def ensure_opencode_project_config(target_dir: Path) -> Path:
    """Create/merge ``opencode.json`` and ensure Superpowers is **not** enabled.

    Superpowers ships ``brainstorming``, which blocks non-interactive WO delivery
    on a human approval gate. Coding-backend skills under ``.opencode/skills/``
    already cover TDD/BDD for this path.
    """
    dest = Path(target_dir)
    dest.mkdir(parents=True, exist_ok=True)
    config_path = dest / "opencode.json"
    data: dict = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    plugins = data.get("plugin")
    if not isinstance(plugins, list):
        plugins = []
    else:
        plugins = [p for p in plugins if not _is_superpowers_plugin_spec(p)]
    data["plugin"] = plugins
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return config_path


# Back-compat alias: historical name installed Superpowers; now strips it.
def ensure_opencode_superpowers_plugin(target_dir: Path) -> Path:
    return ensure_opencode_project_config(target_dir)
