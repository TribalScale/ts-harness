"""Egress allowlist for local coding workers: policy, launcher command and allowlist sync."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.coding import egress
from ts_harness.coding.launcher import (
    CapacityError,
    LocalDockerConfig,
    LocalDockerLauncher,
    launcher_tick,
)

JOB = {"id": "6f1c1f9e-1111-4222-8333-444455556666"}
PROXY_ON = {"LLM_PROXY_SECRET": "s", "LLM_PROXY_URL": "http://127.0.0.1:18795/llm"}


class FakeDocker:
    def __init__(self, *, proxy_ids="egress1\n"):
        self.calls = []
        self.proxy_ids = proxy_ids

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if cmd[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        if cmd[:2] == ["docker", "ps"] and any(egress.PROXY_LABEL in part for part in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=self.proxy_ids, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def commands(self, prefix):
        return [cmd for cmd, _ in self.calls if cmd[: len(prefix)] == prefix]


def _env_args(cmd):
    return {cmd[i + 1].split("=", 1)[0]: cmd[i + 1].split("=", 1)[1] for i, part in enumerate(cmd) if part == "-e" and "=" in cmd[i + 1]}


class AllowlistTests(unittest.TestCase):
    def test_defaults_cover_github_and_package_registries_without_providers_behind_the_llm_proxy(self):
        domains = egress.allowlist(PROXY_ON)
        for domain in ("github.com", "api.github.com", "codeload.github.com", ".githubusercontent.com",
                       "registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
                       "proxy.golang.org", "nodejs.org"):
            self.assertIn(domain, domains)
        # objects.githubusercontent.com is covered by the wildcard; squid refuses both together.
        self.assertNotIn("objects.githubusercontent.com", domains)
        for provider in egress.PROVIDER_DOMAINS:
            self.assertNotIn(provider, domains)
        self.assertEqual(domains, sorted(domains))

    def test_provider_domains_are_added_when_workers_call_providers_directly(self):
        domains = egress.allowlist({"MODEL_BASE_URL": "https://llm.internal.example:8443/v1"})
        for provider in (*egress.PROVIDER_DOMAINS, "llm.internal.example"):
            self.assertIn(provider, domains)

    def test_extra_domains_and_registry_hosts_are_normalized_and_collapsed(self):
        env = {**PROXY_ON, "CODING_EGRESS_ALLOW": " *.Example.org, api.example.org, https://dl.example.net/x , bad domain!, "}
        domains = egress.allowlist(env, ["npm.pkg.example.com", "https://pypi.example.com/simple", ""])
        self.assertIn(".example.org", domains)
        self.assertNotIn("api.example.org", domains)
        for host in ("dl.example.net", "npm.pkg.example.com", "pypi.example.com"):
            self.assertIn(host, domains)
        self.assertFalse([d for d in domains if " " in d or "!" in d])

    def test_internal_services_never_go_through_the_proxy(self):
        domains = egress.allowlist({**PROXY_ON, "CODING_EGRESS_ALLOW": "agent-runtime"})
        self.assertNotIn("agent-runtime", domains)

    def test_normalize_domain(self):
        self.assertEqual(egress.normalize_domain("*.GitHub.com"), ".github.com")
        self.assertEqual(egress.normalize_domain("registry.example.com:8443"), "registry.example.com")
        self.assertEqual(egress.normalize_domain("https://r.example.com/npm/"), "r.example.com")
        self.assertEqual(egress.normalize_domain("not a host"), "")
        self.assertEqual(egress.collapse(["github.com", ".github.com", "api.github.com"]), [".github.com"])

    def test_rendered_file_is_one_domain_per_line_with_a_comment_header(self):
        text = egress.render_domains_file(["a.example.com", ".b.example.com"])
        lines = text.splitlines()
        self.assertTrue(all(line.startswith("#") for line in lines[:2]))
        self.assertEqual(lines[2:], ["a.example.com", ".b.example.com"])

    def test_flag_values(self):
        for value in ("on", "true", "1", "YES"):
            self.assertTrue(egress.enabled({"CODING_EGRESS_ALLOWLIST": value}))
        for value in ("", "off", "false", "0"):
            self.assertFalse(egress.enabled({"CODING_EGRESS_ALLOWLIST": value}))


class LauncherCommandTests(unittest.TestCase):
    def test_off_keeps_the_shared_namespace_and_sets_no_proxy_env(self):
        cfg = LocalDockerConfig.from_env({"CODING_WORKER_NETWORK": "container:abc"})
        self.assertFalse(cfg.egress_allowlist)
        cmd = LocalDockerLauncher(cfg, run=FakeDocker(), environ=PROXY_ON).command(JOB)
        env = _env_args(cmd)
        self.assertIn("--network container:abc", " ".join(cmd))
        self.assertEqual(env["MEMORY_API_URL"], "http://127.0.0.1:18792")
        self.assertEqual(env["HARNESS_RUNTIME_URL"], "http://127.0.0.1:18789")
        self.assertEqual(env["LLM_PROXY_URL"], "http://127.0.0.1:18795/llm")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy"):
            self.assertNotIn(name, env)

    def test_on_joins_the_internal_network_with_proxy_env_and_internal_urls(self):
        environ = {**PROXY_ON, "CODING_EGRESS_ALLOWLIST": "on", "CODING_WORKER_NETWORK": "auto"}
        cfg = LocalDockerConfig.from_env(environ)
        self.assertTrue(cfg.egress_allowlist)
        cmd = LocalDockerLauncher(cfg, run=FakeDocker(), environ=environ).command(JOB)
        joined = " ".join(cmd)
        self.assertIn(f"--network {egress.DEFAULT_NETWORK}", joined)
        self.assertNotIn("container:", joined)
        env = _env_args(cmd)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env[name], "http://egress-proxy:3128")
        no_proxy = env["NO_PROXY"].split(",")
        for host in ("localhost", "127.0.0.1", "::1", "agent-runtime"):
            self.assertIn(host, no_proxy)
        self.assertEqual(env["no_proxy"], env["NO_PROXY"])
        self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")
        self.assertEqual(env["MEMORY_API_URL"], "http://agent-runtime:18792")
        self.assertEqual(env["HARNESS_RUNTIME_URL"], "http://agent-runtime:18789")
        self.assertEqual(env["LLM_PROXY_URL"], "http://agent-runtime:18795/llm")
        self.assertEqual(cmd.count("--network"), 1)

    def test_custom_network_proxy_and_bypass(self):
        environ = {
            "CODING_EGRESS_ALLOWLIST": "on",
            "CODING_EGRESS_NETWORK": "net-x",
            "CODING_EGRESS_PROXY_WORKER_URL": "http://squid:3128",
            "CODING_EGRESS_NO_PROXY": "db.internal",
            "CODING_WORKER_MEMORY_API_URL": "http://memory.example:18792",
        }
        cmd = LocalDockerLauncher(LocalDockerConfig.from_env(environ), run=FakeDocker(), environ=environ).command(JOB)
        env = _env_args(cmd)
        self.assertIn("--network net-x", " ".join(cmd))
        self.assertEqual(env["HTTPS_PROXY"], "http://squid:3128")
        self.assertIn("db.internal", env["NO_PROXY"].split(","))
        # A non-loopback URL is left alone.
        self.assertEqual(env["MEMORY_API_URL"], "http://memory.example:18792")

    def test_start_waits_while_the_proxy_is_not_running(self):
        environ = {"CODING_EGRESS_ALLOWLIST": "on"}
        launcher = LocalDockerLauncher(LocalDockerConfig.from_env(environ), run=FakeDocker(proxy_ids=""), environ=environ)
        with self.assertRaises(CapacityError):
            launcher.start(JOB, "tok")


class FakeMemory:
    def __init__(self, hosts=(), fail=False):
        self.hosts = list(hosts)
        self.fail = fail
        self.calls = 0

    def registry_hosts(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("memory-api down")
        return self.hosts

    def claim_coding_jobs(self, **_):
        return {"jobs": []}

    def list_coding_jobs(self, **_):
        return []


class SyncTests(unittest.TestCase):
    def _launcher(self, tmp, docker, **extra):
        environ = {**PROXY_ON, "CODING_EGRESS_ALLOWLIST": "on", "CODING_EGRESS_ALLOWLIST_PATH": f"{tmp}/domains.txt", **extra}
        return LocalDockerLauncher(LocalDockerConfig.from_env(environ), run=docker, environ=environ)

    def test_writes_the_allowlist_and_reloads_squid_only_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            docker = FakeDocker()
            launcher = self._launcher(tmp, docker, CODING_EGRESS_ALLOW="extra.example.com")
            memory = FakeMemory(["npm.pkg.example.com"])
            first = launcher.sync_egress(memory, now=1000)
            text = Path(tmp, "domains.txt").read_text()
            self.assertIn("extra.example.com\n", text)
            self.assertIn("npm.pkg.example.com\n", text)
            self.assertIn("github.com\n", text)
            self.assertEqual(Path(tmp, "domains.txt").stat().st_mode & 0o777, 0o644)
            self.assertTrue(first["changed"] and first["reloaded"])
            self.assertEqual(docker.commands(["docker", "kill"]), [["docker", "kill", "-s", "HUP", "egress1"]])
            # Within the interval nothing happens; after it, an unchanged list sends no signal.
            self.assertIsNone(launcher.sync_egress(memory, now=1010))
            second = launcher.sync_egress(memory, now=1100)
            self.assertFalse(second["changed"])
            self.assertEqual(len(docker.commands(["docker", "kill"])), 1)
            # A new registry host is picked up and reloads squid.
            memory.hosts.append("maven.example.com")
            self.assertTrue(launcher.sync_egress(memory, now=1200)["reloaded"])
            self.assertIn("maven.example.com\n", Path(tmp, "domains.txt").read_text())

    def test_keeps_the_last_registry_hosts_when_memory_api_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = self._launcher(tmp, FakeDocker())
            launcher.sync_egress(FakeMemory(["npm.pkg.example.com"]), now=1000)
            result = launcher.sync_egress(FakeMemory(fail=True), now=2000)
            self.assertFalse(result["changed"])
            self.assertIn("npm.pkg.example.com\n", Path(tmp, "domains.txt").read_text())

    def test_off_is_a_no_op(self):
        docker = FakeDocker()
        launcher = LocalDockerLauncher(LocalDockerConfig(), run=docker, environ={})
        self.assertIsNone(launcher.sync_egress(FakeMemory(), now=1000))
        self.assertEqual(docker.calls, [])

    def test_tick_syncs_before_claiming(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = self._launcher(tmp, FakeDocker())
            memory = FakeMemory()
            launcher_tick(memory, launcher, max_running=1)
            self.assertEqual(memory.calls, 1)
            self.assertTrue(Path(tmp, "domains.txt").exists())


class CheckTests(unittest.TestCase):
    def test_check_passes_when_allowed_answers_and_blocked_fails(self):
        def fetch(url, _timeout):
            return ("example.com" not in url, "HTTP 200" if "example.com" not in url else "Tunnel connection failed: 403")

        ok, results = egress.run_check(
            allowed=["https://api.github.com/"], blocked=["https://example.com/"],
            internal=["http://agent-runtime:18792/health"], direct=["1.1.1.1:443"],
            environ={"HTTPS_PROXY": "http://egress-proxy:3128"},
            fetch=fetch, connect=lambda *_: (False, "unreachable"),
        )
        self.assertTrue(ok)
        self.assertEqual([r["check"] for r in results], ["proxy_configured", "allowed", "internal", "blocked", "direct_blocked"])

    def test_check_fails_when_a_blocked_host_answers_or_no_proxy_is_set(self):
        ok, results = egress.run_check(
            allowed=[], blocked=["https://example.com/"], internal=[], direct=["1.1.1.1:443"], environ={},
            fetch=lambda *_: (True, "HTTP 200"), connect=lambda *_: (True, "connected"),
        )
        self.assertFalse(ok)
        self.assertEqual([r["ok"] for r in results], [False, False, False])


if __name__ == "__main__":
    unittest.main()
