"""Package manager config written for a coding job's registries."""

from __future__ import annotations

import base64
import json
import stat
import tempfile
import unittest
from pathlib import Path

from ts_harness.coding import worker
from ts_harness.coding.registries import write_registry_config
from tests.runtime.test_coding_worker import FakeApi


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class WriteRegistryConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_every_type_written_private(self):
        configured = write_registry_config(
            [
                {"type": "npm", "url": "https://npm.acme.dev/repo", "scope": "acme", "token": "nt"},
                {"type": "npm", "url": "https://npm.other.dev", "username": "u", "token": "pw"},
                {"type": "pypi", "url": "https://pypi.acme.dev/simple", "token": "p@ss"},
                {"type": "pypi", "url": "https://public.acme.dev/simple"},
                {"type": "maven", "url": "https://m.acme.dev/releases", "scope": "acme-releases", "username": "ci", "token": "<mv>"},
                {"type": "maven", "url": "https://m.acme.dev/all", "token": "mv2"},
                {"type": "docker", "url": "https://reg.acme.dev", "username": "bot", "token": "dk"},
            ],
            self.home,
        )
        self.assertEqual(configured, ["docker", "maven", "npm", "pypi"])
        files = [".npmrc", ".config/pip/pip.conf", ".m2/settings.xml", ".docker/config.json"]
        for name in files:
            self.assertEqual(_mode(self.home / name), 0o600, name)

        npmrc = (self.home / ".npmrc").read_text()
        self.assertIn("@acme:registry=https://npm.acme.dev/repo/", npmrc)
        self.assertIn("//npm.acme.dev/repo/:_authToken=nt", npmrc)
        self.assertIn(f"//npm.other.dev/:_auth={base64.b64encode(b'u:pw').decode()}", npmrc)

        pip = (self.home / ".config/pip/pip.conf").read_text()
        self.assertIn("index-url = https://__token__:p%40ss@pypi.acme.dev/simple", pip)
        self.assertIn("    https://public.acme.dev/simple", pip)

        maven = (self.home / ".m2/settings.xml").read_text()
        self.assertIn("<id>acme-releases</id>", maven)
        self.assertIn("<password>&lt;mv&gt;</password>", maven)
        self.assertEqual(maven.count("<mirrorOf>*</mirrorOf>"), 1)
        self.assertIn("<url>https://m.acme.dev/all</url>", maven)

        docker = json.loads((self.home / ".docker/config.json").read_text())
        self.assertEqual(docker["auths"]["reg.acme.dev"]["auth"], base64.b64encode(b"bot:dk").decode())

    def test_nothing_written_without_registries(self):
        self.assertEqual(write_registry_config([{"type": "npm"}], self.home), [])
        self.assertEqual(list(self.home.iterdir()), [])


class WorkerRegistrySetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_registries_configured_before_the_run(self):
        api = FakeApi(spec={"workspace_dir": str(self.home)})
        api.registry_list = [{"type": "npm", "url": "https://npm.acme.dev", "token": "t"}]
        seen = []
        code = worker.run_job(api, run=lambda spec: seen.append((self.home / ".npmrc").exists()) or {"ok": True},
                              heartbeat_seconds=60, home=self.home)
        self.assertEqual((code, seen), (0, [True]))

    def test_setup_failure_completes_the_job_with_an_error(self):
        api = FakeApi(spec={"workspace_dir": str(self.home)})
        api.registries = lambda: (_ for _ in ()).throw(RuntimeError("secret unavailable"))
        code = worker.run_job(api, run=lambda spec: {"ok": True}, heartbeat_seconds=60, home=self.home)
        self.assertEqual(code, 1)
        self.assertIn("package registry setup failed", api.completed[0][1])


if __name__ == "__main__":
    unittest.main()
