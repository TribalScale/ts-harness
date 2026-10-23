"""SPA redirects and gateway proxying for the admin console nginx config."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONF = REPO_ROOT / "docker" / "nginx-admin-ui.conf"
ASGI_APP = REPO_ROOT / "runtime" / "ts_harness" / "gateway" / "asgi_app.py"


def _proxied_prefixes(conf: str) -> set[str]:
    prefixes = {m.strip("/") for m in re.findall(r"location (/[a-z]+)/?\s*\{", conf)}
    for group in re.findall(r"location ~ \^/\(([^)]*)\)", conf):
        prefixes.update(group.split("|"))
    return prefixes


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

    def test_every_gateway_route_prefix_is_proxied(self):
        app = ASGI_APP.read_text(encoding="utf-8")
        gateway = set(re.findall(r'Route\("/([a-z]+)', app))
        gateway.update(re.findall(r'Mount\(\s*"/([a-z]+)"', app))
        self.assertIn("oauth", gateway)
        self.assertIn("channels", gateway)
        missing = gateway - _proxied_prefixes(CONF.read_text(encoding="utf-8"))
        self.assertEqual(missing, set())

    def test_agent_runtime_is_re_resolved_after_container_recreate(self):
        text = CONF.read_text(encoding="utf-8")
        self.assertIn("resolver 127.0.0.11", text)
        self.assertIn("server agent-runtime:18789 resolve;", text)
        self.assertNotIn("proxy_pass http://agent-runtime", text)
        self.assertNotIn("proxy_pass http://$", text)

    def test_runtime_replicas_share_the_upstream(self):
        text = CONF.read_text(encoding="utf-8")
        upstream = re.search(r"upstream agent_runtime \{(.*?)\}", text, re.S).group(1)
        self.assertIn("zone agent_runtime", upstream)
        self.assertIn("server agent-runtime-2:18789 resolve;", upstream)
        self.assertEqual(text.count("proxy_pass http://agent_runtime;"), text.count("proxy_pass"))


class BootstrapAdminConsoleTests(unittest.TestCase):
    def test_compose_phase_starts_admin_ui(self):
        phase = (REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh").read_text()
        self.assertIn("docker-compose.admin-ui.yml", phase)
        self.assertLess(
            phase.index("docker-compose.admin-ui.yml"),
            phase.index("PHASE_FINGERPRINT="),
        )

    def test_tailnet_root_serves_admin_console(self):
        phase = (REPO_ROOT / "scripts" / "phases" / "06-tailscale.sh").read_text()
        self.assertIn("tailscale serve --bg http://localhost:8080", phase)
        self.assertNotIn("tailscale serve --bg http://localhost:${GATEWAY_PORT", phase)


if __name__ == "__main__":
    unittest.main()
