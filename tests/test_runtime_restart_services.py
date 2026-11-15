"""Restarting agent-runtime also restarts the services that run in its network namespace."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"


def services(files: str, *args: str) -> list[str]:
    text = COMMON.read_text()
    start = text.index("with_runtime_replicas() {")
    body = text[start:text.index("\n}\n", start) + 3]
    script = f'{body}\nCOMPOSE_FILES_SERIALIZED="{files}" with_runtime_replicas {" ".join(args)}'
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return done.stdout.split()


class RuntimeRestartServicesTests(unittest.TestCase):
    def test_every_service_in_the_runtime_namespace_comes_along(self):
        files = "docker-compose.agent-runtime.yml:docker-compose.memory-api.yml:docker-compose.workflow-runner.yml"
        self.assertEqual(services(files, "agent-runtime"), ["agent-runtime", "memory-api", "workflow-runner"])

    def test_replicas_proxy_and_coding_jobs_are_included_once(self):
        files = ":".join(f"docker-compose.{name}.yml" for name in
                         ("agent-runtime", "memory-api", "replicas", "llm-proxy", "workflow-runner", "coding-jobs"))
        self.assertEqual(
            services(files, "agent-runtime", "memory-api"),
            ["agent-runtime", "agent-runtime-2", "memory-api", "memory-api-2", "llm-proxy", "workflow-runner", "coding-launcher"],
        )

    def test_every_namespace_sharer_in_compose_is_covered(self):
        sharers = set()
        for path in (REPO_ROOT / "docker").glob("docker-compose.*.yml"):
            if path.name in {"docker-compose.quickstart.yml", "docker-compose.replicas.yml"}:
                continue
            for match in re.finditer(r"\n  ([a-z0-9-]+):\n(?:    .*\n)*?    network_mode: \"service:agent-runtime\"", path.read_text()):
                sharers.add(match.group(1))
        all_files = ":".join(f"docker-compose.{name}.yml" for name in
                             ("agent-runtime", "memory-api", "llm-proxy", "workflow-runner", "coding-jobs"))
        self.assertTrue(sharers)
        self.assertLessEqual(sharers, set(services(all_files, "agent-runtime")))


if __name__ == "__main__":
    unittest.main()
