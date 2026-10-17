"""SPA redirects for bare admin console paths."""

from __future__ import annotations

import unittest
from pathlib import Path


CONF = Path(__file__).resolve().parents[1] / "docker" / "nginx-admin-ui.conf"


class NginxAdminUiTests(unittest.TestCase):
    def test_bare_system_paths_redirect_to_hash_screens(self):
        text = CONF.read_text(encoding="utf-8")
        for path, target in (
            ("/admin/cron", "/#/system/cron"),
            ("/admin/mcp", "/#/system/mcp"),
            ("/admin/settings", "/#/system/settings"),
        ):
            self.assertIn(f'location = {path} {{ return 302 "{target}"; }}', text)
        proxy = text.index("location /admin/")
        self.assertLess(text.index("location = /admin/cron"), proxy)
        self.assertLess(text.index("location = /admin/mcp"), proxy)
        self.assertLess(text.index("location = /admin/settings"), proxy)
