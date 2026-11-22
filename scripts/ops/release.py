#!/usr/bin/env python3
"""Cut a TS Harness release: dev -> main promotion, version bump, changelog, tag.

    python3 scripts/ops/release.py prepare 0.2.0   # release/v0.2.0 from origin/dev, PR into main
    python3 scripts/ops/release.py tag 0.2.0       # after the PR merged: tag main, GitHub release
    python3 scripts/ops/release.py notes 0.2.0     # print the changelog section

``prepare`` renames the changelog's Unreleased section to the version and date, starts a new
empty Unreleased section, bumps ``pyproject.toml`` and ``admin-ui/package.json``, commits on
``release/v<version>`` and opens a PR into ``main``. ``tag`` tags the merged release commit on
``main`` (``v<version>``), which starts the Release workflow, and creates the GitHub release with
the changelog section as notes. See docs/releasing.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
UNRELEASED = "## [Unreleased]"


def cut_changelog(text: str, version: str, date: str) -> str:
    """Rename Unreleased to ``version`` and start a new empty Unreleased section above it."""
    if UNRELEASED not in text:
        raise ValueError("CHANGELOG.md has no '## [Unreleased]' section")
    if f"## [{version}]" in text:
        raise ValueError(f"CHANGELOG.md already has a {version} section")
    return text.replace(UNRELEASED, f"{UNRELEASED}\n\n## [{version}] - {date}", 1)


def section(text: str, version: str) -> str:
    """The changelog section for ``version`` without its heading."""
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    if not match:
        raise ValueError(f"CHANGELOG.md has no {version} section")
    return match.group(1).strip() + "\n"


def bump_pyproject(text: str, version: str) -> str:
    updated, count = re.subn(r'^version = "[^"]*"', f'version = "{version}"', text, count=1, flags=re.M)
    if count != 1:
        raise ValueError("pyproject.toml has no version line")
    return updated


def bump_package_json(text: str, version: str) -> str:
    data = json.loads(text)
    data["version"] = version
    return json.dumps(data, indent=2) + "\n"


def _run(*cmd: str, capture: bool = False) -> str:
    result = subprocess.run(cmd, cwd=ROOT, check=True, text=True, capture_output=capture)
    return (result.stdout or "").strip() if capture else ""


def prepare(version: str) -> None:
    if _run("git", "status", "--porcelain", capture=True):
        raise SystemExit("the working tree has changes; commit or stash them first")
    _run("git", "fetch", "origin", "dev", "main", "--tags")
    if _run("git", "tag", "-l", f"v{version}", capture=True):
        raise SystemExit(f"tag v{version} already exists")
    branch = f"release/v{version}"
    _run("git", "checkout", "-B", branch, "origin/dev")
    today = dt.date.today().isoformat()
    changelog = ROOT / "CHANGELOG.md"
    changelog.write_text(cut_changelog(changelog.read_text(encoding="utf-8"), version, today), encoding="utf-8")
    pyproject = ROOT / "pyproject.toml"
    pyproject.write_text(bump_pyproject(pyproject.read_text(encoding="utf-8"), version), encoding="utf-8")
    package = ROOT / "admin-ui" / "package.json"
    package.write_text(bump_package_json(package.read_text(encoding="utf-8"), version), encoding="utf-8")
    _run("git", "add", "CHANGELOG.md", "pyproject.toml", "admin-ui/package.json")
    _run("git", "commit", "-m", f"Release v{version}")
    _run("git", "push", "-u", "origin", branch)
    notes = section(changelog.read_text(encoding="utf-8"), version)
    body = f"Promotes dev to main as v{version}.\n\n{notes}"
    try:
        _run("gh", "pr", "create", "--base", "main", "--head", branch, "--title", f"Release v{version}", "--body", body)
    except (OSError, subprocess.CalledProcessError):
        print(f"Open a PR from {branch} into main titled 'Release v{version}'.")
    print(f"Prepared {branch}. After it merges into main: python3 scripts/ops/release.py tag {version}")


def tag(version: str) -> None:
    _run("git", "fetch", "origin", "main", "--tags")
    main_version = _run("git", "show", "origin/main:pyproject.toml", capture=True)
    if f'version = "{version}"' not in main_version:
        raise SystemExit(f"origin/main is not at {version}; merge the release PR first")
    _run("git", "tag", "-a", f"v{version}", "origin/main", "-m", f"v{version}")
    _run("git", "push", "origin", f"v{version}")
    notes = section(_run("git", "show", "origin/main:CHANGELOG.md", capture=True), version)
    try:
        _run("gh", "release", "create", f"v{version}", "--title", f"v{version}", "--notes", notes)
    except (OSError, subprocess.CalledProcessError):
        print("Create the GitHub release from the tag and paste the changelog section.")
    print(f"Tagged v{version}. Hosts roll forward or back with scripts/ops/rollback.sh v{version}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["prepare", "tag", "notes"])
    parser.add_argument("version")
    args = parser.parse_args(argv)
    if not VERSION_RE.match(args.version):
        raise SystemExit("version must look like 0.2.0")
    if args.action == "prepare":
        prepare(args.version)
    elif args.action == "tag":
        tag(args.version)
    else:
        print(section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), args.version), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
