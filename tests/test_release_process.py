"""Release tooling and the additive-migration rule that makes rollbacks safe."""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_tool", ROOT / "scripts" / "ops" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Thing one.

## [0.1.0] - 2026-01-01

### Added

- First.
"""

# Migrations up to this number predate the rule.
GRANDFATHERED_UP_TO = 50
DESTRUCTIVE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bRENAME\s+(?:COLUMN|TO)\b|\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b|\bDROP\s+TYPE\b"
    r"|\bTRUNCATE\b",
    re.IGNORECASE,
)


class ReleaseToolTests(unittest.TestCase):
    def test_cut_changelog_starts_a_new_unreleased_section(self):
        cut = release.cut_changelog(CHANGELOG, "0.2.0", "2026-02-01")
        self.assertIn("## [Unreleased]\n\n## [0.2.0] - 2026-02-01\n\n### Added\n\n- Thing one.", cut)
        self.assertEqual(release.section(cut, "0.2.0"), "### Added\n\n- Thing one.\n")
        self.assertEqual(release.section(cut, "0.1.0"), "### Added\n\n- First.\n")
        with self.assertRaises(ValueError):
            release.cut_changelog(cut, "0.2.0", "2026-02-02")

    def test_version_bumps(self):
        self.assertIn('version = "0.2.0"', release.bump_pyproject('[project]\nname = "x"\nversion = "0.1.0"\n', "0.2.0"))
        self.assertEqual(json.loads(release.bump_package_json('{"name": "ui", "version": "0.1.0"}', "0.2.0"))["version"], "0.2.0")

    def test_versions_match_today(self):
        pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        package = json.loads((ROOT / "admin-ui" / "package.json").read_text())["version"]
        self.assertEqual(pyproject, package)

    def test_bad_version_is_refused(self):
        with self.assertRaises(SystemExit):
            release.main(["notes", "v1"])


class AdditiveMigrationTests(unittest.TestCase):
    def test_new_migrations_are_additive_unless_marked(self):
        """Rollback runs an older release on the newer schema, so new migrations only add.

        A migration that must remove or retype something says why on a ``-- destructive:`` line
        and ships with a release note on how to roll back.
        """
        offending = []
        for path in sorted((ROOT / "database" / "migrations").glob("*.sql")):
            number = int(path.name.split("_", 1)[0])
            if number <= GRANDFATHERED_UP_TO:
                continue
            text = path.read_text(encoding="utf-8")
            if "-- destructive:" in text:
                continue
            code = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
            if DESTRUCTIVE.search(code):
                offending.append(path.name)
        self.assertEqual(offending, [], "destructive migration without a '-- destructive: <reason>' line")


if __name__ == "__main__":
    unittest.main()
