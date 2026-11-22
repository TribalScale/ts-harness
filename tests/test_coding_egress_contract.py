"""Coding worker egress allowlist: compose overlay, deploy wiring and docs."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER = REPO_ROOT / "docker"
OVERLAY = DOCKER / "docker-compose.coding-egress.yml"
SQUID_CONF = DOCKER / "egress-proxy" / "squid.conf"
PHASE_12 = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
ROTATE = REPO_ROOT / "scripts" / "ops" / "rotate-secrets.sh"
OPS = REPO_ROOT / "scripts" / "ops" / "coding-egress.sh"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "implement-work-orders-opencode.md"


def _overlay() -> dict:
    return yaml.safe_load(OVERLAY.read_text())


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    return text[start:text.index("\n}\n", start) + 3]


def _wanted(**env: str) -> bool:
    body = _function(COMMON.read_text(), "coding_egress_allowlist_wanted")
    assignments = " ".join(f"{k}={v!r}" for k, v in env.items())
    script = f"{body}\n{assignments} coding_egress_allowlist_wanted"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode == 0


class OverlayTests(unittest.TestCase):
    def test_worker_network_is_internal_and_named(self):
        network = _overlay()["networks"]["coding-egress"]
        self.assertTrue(network["internal"])
        self.assertEqual(network["name"], "${CODING_EGRESS_NETWORK:-ts-harness-coding-egress}")

    def test_proxy_is_pinned_labelled_and_bridges_the_two_networks(self):
        proxy = _overlay()["services"]["egress-proxy"]
        image = proxy["image"]
        self.assertIn("squid:", image)
        tag = image.rsplit(":", 1)[1]
        self.assertNotIn(tag.split("}")[0], ("latest", ""))
        self.assertRegex(tag, r"^\d+\.\d+")
        self.assertEqual(proxy["labels"]["ts-harness.egress-proxy"], "true")
        self.assertEqual(set(proxy["networks"]), {"default", "coding-egress"})
        self.assertIn("ts-harness-egress-allowlist:/etc/squid/allowlist:ro", proxy["volumes"])
        self.assertTrue(any(v.endswith("/docker/egress-proxy/squid.conf:/etc/squid/egress.conf:ro") for v in proxy["volumes"]))
        self.assertIn("exec squid -N -f /etc/squid/egress.conf", proxy["command"][0])
        self.assertNotIn("ports", proxy)

    def test_runtime_joins_the_worker_network_and_memory_api_listens_beyond_loopback(self):
        services = _overlay()["services"]
        self.assertEqual(set(services["agent-runtime"]["networks"]), {"default", "coding-egress"})
        self.assertEqual(services["memory-api"]["environment"]["MEMORY_API_BIND"], "0.0.0.0")
        self.assertNotIn("ports", services["agent-runtime"])

    def test_launcher_turns_the_allowlist_on_and_shares_the_allowlist_volume(self):
        launcher = _overlay()["services"]["coding-launcher"]
        env = launcher["environment"]
        self.assertEqual(env["CODING_EGRESS_ALLOWLIST"], "on")
        self.assertEqual(env["CODING_EGRESS_ALLOW"], "${CODING_EGRESS_ALLOW:-}")
        self.assertEqual(env["CODING_EGRESS_ALLOWLIST_PATH"], "/egress/domains.txt")
        self.assertIn("ts-harness-egress-allowlist:/egress", launcher["volumes"])
        self.assertIn("egress-proxy", launcher["depends_on"])
        self.assertIn("ts-harness-egress-allowlist", _overlay()["volumes"])

    def test_base_stack_is_unchanged_without_the_overlay(self):
        for name in ("docker-compose.agent-runtime.yml", "docker-compose.coding-jobs.yml", "docker-compose.memory-api.yml"):
            data = yaml.safe_load((DOCKER / name).read_text())
            self.assertNotIn("egress-proxy", data.get("services", {}))
            self.assertNotIn("coding-egress", data.get("networks") or {})
        launcher = yaml.safe_load((DOCKER / "docker-compose.coding-jobs.yml").read_text())["services"]["coding-launcher"]
        self.assertNotIn("CODING_EGRESS_ALLOWLIST", launcher["environment"])
        self.assertEqual(launcher["environment"]["CODING_WORKER_NETWORK"], "auto")

    def test_squid_allows_only_the_allowlist_and_logs_denials(self):
        conf = SQUID_CONF.read_text()
        self.assertIn('acl allowed dstdomain "/etc/squid/allowlist/domains.txt"', conf)
        self.assertIn("http_access deny CONNECT !ssl_ports", conf)
        lines = [line for line in conf.splitlines() if line.startswith("http_access")]
        self.assertEqual(lines[-2:], ["http_access allow allowed", "http_access deny all"])
        self.assertIn("access_log stdio:/dev/stdout", conf)
        self.assertIn("http_port 3128", conf)


class DeployWiringTests(unittest.TestCase):
    def test_phase_12_includes_the_overlay_when_wanted_before_the_fingerprint(self):
        phase = PHASE_12.read_text()
        gate = phase.index("if coding_egress_allowlist_wanted; then")
        self.assertLess(phase.index("docker-compose.coding-jobs.yml"), gate)
        self.assertLess(gate, phase.index("docker-compose.coding-egress.yml"))
        self.assertLess(phase.index("docker-compose.coding-egress.yml"), phase.index("PHASE_FINGERPRINT="))
        self.assertIn('"$EGRESS_FINGERPRINT"', phase)
        for name in ("CODING_EGRESS_ALLOWLIST", "CODING_EGRESS_ALLOW"):
            self.assertIn(f'echo "export {name}=', phase)

    def test_flag_is_persisted_and_reaches_compose(self):
        common = COMMON.read_text()
        keys = common[common.index("INSTANCE_ENV_KEYS=("):]
        keys = keys[:keys.index(")")]
        for name in ("CODING_EGRESS_ALLOWLIST", "CODING_EGRESS_ALLOW"):
            self.assertIn(f"    {name}\n", keys)
        compose = _function(common, "run_docker_compose_as_admin")
        for name in ("CODING_EGRESS_ALLOW=", "CODING_EGRESS_NETWORK=", "CODING_EGRESS_PROXY_IMAGE="):
            self.assertIn(name, compose)

    def test_wanted_only_for_local_coding_jobs_with_the_flag_on(self):
        jobs = {"WORKFLOW_RUNNER_ENABLED": "true", "CODING_JOBS_ENABLED": "true"}
        self.assertTrue(_wanted(CODING_EGRESS_ALLOWLIST="on", **jobs))
        self.assertTrue(_wanted(CODING_EGRESS_ALLOWLIST="TRUE", CODING_LAUNCHER="local", DEPLOYMENT_PROFILE="scale_out", **jobs))
        self.assertFalse(_wanted(**jobs))
        self.assertFalse(_wanted(CODING_EGRESS_ALLOWLIST="off", **jobs))
        self.assertFalse(_wanted(CODING_EGRESS_ALLOWLIST="on", WORKFLOW_RUNNER_ENABLED="true"))
        self.assertFalse(_wanted(CODING_EGRESS_ALLOWLIST="on", DEPLOYMENT_PROFILE="scale_out", **jobs))
        self.assertFalse(_wanted(CODING_EGRESS_ALLOWLIST="on", CODING_LAUNCHER="ecs", **jobs))

    def test_secret_rotation_keeps_the_overlay(self):
        rotate = _function(ROTATE.read_text(), "reconstruct_enterprise_compose_files")
        self.assertIn("coding_egress_allowlist_wanted", rotate)
        self.assertIn("docker-compose.coding-egress.yml", rotate)

    def test_ops_check_script(self):
        text = OPS.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", text)
        self.assertIn("python3 -m ts_harness.coding.egress check", text)
        self.assertIn("TCP_DENIED", text)
        subprocess.run(["bash", "-n", str(OPS)], check=True)


class DocsTests(unittest.TestCase):
    def test_runbook_documents_enable_verify_and_denials(self):
        text = RUNBOOK.read_text()
        coding = text[text.index("## Coding jobs"):text.index("## Projects with several repositories")]
        for needle in ("CODING_EGRESS_ALLOWLIST=on", "CODING_EGRESS_ALLOW", "egress-proxy",
                       "scripts/ops/coding-egress.sh check", "scripts/ops/coding-egress.sh denied", "TCP_DENIED"):
            self.assertIn(needle, coding)

    def test_changelog_mentions_it(self):
        self.assertIn("CODING_EGRESS_ALLOWLIST", (REPO_ROOT / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
