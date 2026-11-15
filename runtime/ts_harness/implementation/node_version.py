"""Run a repo on the Node.js major it pins, not only the one the worker image ships.

The pin comes from ``.nvmrc``, ``.node-version``, ``.tool-versions``,
``package.json`` ``volta.node`` or ``engines.node``, in that order. When the
image's Node satisfies it nothing happens; otherwise the newest release of the
pinned major (or the exact version) is downloaded from nodejs.org once per
host cache, checked against the release's SHASUMS256.txt, and put first on the
job's PATH.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

DIST = "https://nodejs.org/dist"
LTS_CODENAMES = {
    "argon": 4, "boron": 6, "carbon": 8, "dubnium": 10, "erbium": 12, "fermium": 14,
    "gallium": 16, "hydrogen": 18, "iron": 20, "jod": 22, "krypton": 24,
}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pinned_node(repo_dir: Path) -> tuple[str, str]:
    """(spec, where it came from), or ("", "") when the repo pins nothing."""
    repo_dir = Path(repo_dir)
    for name in (".nvmrc", ".node-version"):
        text = _read(repo_dir / name).splitlines()
        if text and text[0].strip():
            return text[0].strip(), name
    for line in _read(repo_dir / ".tool-versions").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"nodejs", "node"}:
            return parts[1], ".tool-versions"
    try:
        package = json.loads(_read(repo_dir / "package.json") or "{}")
    except json.JSONDecodeError:
        package = {}
    volta = (package.get("volta") or {}).get("node") if isinstance(package.get("volta"), dict) else None
    if volta:
        return str(volta), "package.json volta.node"
    engines = (package.get("engines") or {}).get("node") if isinstance(package.get("engines"), dict) else None
    if engines:
        return str(engines), "package.json engines.node"
    return "", ""


def _version(text: str) -> tuple[int, int, int] | None:
    match = re.match(r"^v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?$", text.strip())
    if not match:
        return None
    nums = [int(g) if g and g.isdigit() else -1 for g in match.groups()]
    return nums[0], nums[1], nums[2]


def _satisfies_clause(version: tuple[int, int, int], clause: str) -> bool:
    """One space-separated range clause: ``>=18``, ``^20.1``, ``~20.1.0``, ``20.x``, ``20``."""
    for token in clause.split():
        match = re.match(r"^(>=|<=|>|<|\^|~|=)?\s*(.+)$", token)
        if not match:
            return False
        op, raw = match.group(1) or "", match.group(2)
        want = _version(raw)
        if want is None:
            return False
        major, minor, patch = want
        floor = (major, max(minor, 0), max(patch, 0))
        if op == ">=" and version < floor:
            return False
        if op == ">" and version <= floor:
            return False
        if op == "<" and version >= floor:
            return False
        if op == "<=" and version > (major, minor if minor >= 0 else 10**6, patch if patch >= 0 else 10**6):
            return False
        if op in {"^", ""} or op == "=":
            if version[0] != major or (op in {"", "="} and minor >= 0 and version[1] != minor) or (
                op in {"", "="} and patch >= 0 and version[2] != patch
            ):
                return False
            if op == "^" and version < floor:
                return False
        if op == "~" and (version[:2] != (major, max(minor, 0)) or version < floor):
            return False
    return True


def satisfies(version: str, spec: str) -> bool:
    current = _version(version)
    if current is None:
        return False
    spec = spec.strip()
    if spec.lower().startswith("lts/"):
        name = spec[4:].lower()
        return name in {"*", ""} or LTS_CODENAMES.get(name) == current[0]
    if spec.lower() in {"node", "latest", "current", "stable", "*", ""}:
        return True
    return any(_satisfies_clause(current, clause.strip()) for clause in spec.split("||"))


def wanted_major(spec: str) -> int | None:
    """The major to install when the image's Node does not satisfy ``spec``."""
    spec = spec.strip().lower()
    if spec.startswith("lts/"):
        return LTS_CODENAMES.get(spec[4:])
    clauses = [clause.strip() for clause in spec.split("||") if clause.strip()]
    majors = []
    for clause in clauses:
        for token in clause.split():
            match = re.match(r"^(\^|~|=|>=)?v?(\d+)", token)
            if match:
                majors.append(int(match.group(2)))
                break
    return max(majors) if majors else None


def _arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def _fetch(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed nodejs.org origin
        return response.read()


def resolve_release(spec: str, *, fetch=_fetch) -> str | None:
    """The newest ``vX.Y.Z`` on nodejs.org that satisfies ``spec``."""
    exact = _version(spec)
    if exact and min(exact) >= 0:
        return f"v{exact[0]}.{exact[1]}.{exact[2]}"
    index = json.loads(fetch(f"{DIST}/index.json"))
    major = wanted_major(spec)
    for release in index:
        version = str(release.get("version") or "")
        if satisfies(version, spec) and (major is None or version.startswith(f"v{major}.")):
            return version
    return None


def install_node(version: str, cache_dir: Path, *, fetch=_fetch) -> Path:
    """``bin`` of Node ``version`` under ``cache_dir``; downloads and verifies it once."""
    arch = _arch()
    name = f"node-{version}-linux-{arch}"
    home = Path(cache_dir) / name
    if (home / "bin" / "node").exists():
        return home / "bin"
    archive = f"{name}.tar.xz"
    sums = fetch(f"{DIST}/{version}/SHASUMS256.txt").decode()
    expected = next((line.split()[0] for line in sums.splitlines() if line.endswith(f"  {archive}")), "")
    if not expected:
        raise RuntimeError(f"{archive} is not listed in SHASUMS256.txt")
    data = fetch(f"{DIST}/{version}/{archive}", timeout=300)
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f"{archive} checksum mismatch")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    staging = Path(cache_dir) / f".{name}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    tmp = staging / archive
    tmp.write_bytes(data)
    with tarfile.open(tmp, "r:xz") as tar:
        tar.extractall(staging, filter="data")
    tmp.unlink()
    (staging / name).rename(home)
    shutil.rmtree(staging, ignore_errors=True)
    return home / "bin"


def current_node(env: dict[str, str]) -> str:
    node = shutil.which("node", path=env.get("PATH"))
    if not node:
        return ""
    result = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    return (result.stdout or "").strip()


def ensure_node(repo_dir: Path, env: dict[str, str], cache_dir: Path, *, fetch=_fetch, current: str | None = None) -> tuple[str, str]:
    """(bin dir to put first on PATH or "", log line)."""
    spec, source = pinned_node(repo_dir)
    if not spec or not (Path(repo_dir) / "package.json").is_file():
        return "", ""
    have = current if current is not None else current_node(env)
    exact = _version(spec)
    if exact and min(exact) >= 0:
        # An exact pin (``.nvmrc`` 24.14.1) is met by the same major: patch releases do not
        # change what a test run sees, and a download per job would cost more than it buys.
        same_major = _version(have) is not None and _version(have)[0] == exact[0]
        if same_major:
            return "", ""
    elif have and satisfies(have, spec):
        return "", ""
    try:
        version = resolve_release(spec, fetch=fetch)
        if not version:
            return "", f"node {spec} from {source}: no matching release; using {have or 'the image Node'}"
        bin_dir = install_node(version, cache_dir, fetch=fetch)
    except Exception as exc:  # noqa: BLE001 - fall back to the image Node and say so
        return "", f"node {spec} from {source} unavailable ({exc}); using {have or 'the image Node'}"
    return str(bin_dir), f"node {version} for {spec} from {source} (image has {have or 'none'})"
