"""Every audit action the memory gateway writes must be allowed by the audit log check.

``session_audit_log.action_type`` has a CHECK list. An action missing from it fails the
insert, and with it the request that wrote it, which unit tests with a fake cursor
cannot see.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = "ADD CONSTRAINT session_audit_log_action_type_check"
CALL = re.compile(r"audit_workflow_event\(\s*cur\w*,\s*\"([a-z_]+)\"")


def allowed_actions() -> set[str]:
    migrations = sorted((ROOT / "database" / "migrations").glob("*.sql"))
    latest = [path for path in migrations if CHECK in path.read_text()][-1]
    block = latest.read_text().split(CHECK, 1)[1].split(");", 1)[0]
    return set(re.findall(r"'([a-z_]+)'", block))


def used_actions() -> dict[str, str]:
    files = [
        ROOT / "scripts" / "memory-gateway.py",
        ROOT / "scripts" / "close-ended-programs.py",
        *sorted((ROOT / "scripts" / "memory_routes").glob("*.py")),
    ]
    found: dict[str, str] = {}
    for path in files:
        for match in CALL.finditer(path.read_text()):
            found.setdefault(match.group(1), path.name)
    return found


class AuditActionTypeTests(unittest.TestCase):
    def test_every_written_action_is_allowed(self):
        allowed = allowed_actions()
        used = used_actions()
        self.assertIn("delivery_program_reopened", used)
        self.assertIn("delivery_program_closed_by_backfill", used)
        missing = {action: source for action, source in used.items() if action not in allowed}
        self.assertEqual(missing, {}, "add these to a new migration's session_audit_log check")


if __name__ == "__main__":
    unittest.main()
