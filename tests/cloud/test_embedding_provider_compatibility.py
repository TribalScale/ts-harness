"""Embedding provider compatibility across CLOUD_PROVIDER=aws|gcp."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "scripts" / "phases" / "11e-preflight-embeddings.sh"
EMBED = REPO_ROOT / "scripts" / "embed.sh"
COMPOSE = REPO_ROOT / "docker" / "docker-compose.memory-api.yml"
MEMORY_PREFLIGHT = REPO_ROOT / "scripts" / "preflight-memory-env.sh"

CROSS_CLOUD_PROVIDERS = ("openai", "voyage", "gemini", "mistral")


class EmbeddingProviderCompatibilityTests(unittest.TestCase):
    def test_scripts_and_compose_exist(self):
        self.assertTrue(PREFLIGHT.is_file())
        self.assertTrue(EMBED.is_file())
        self.assertTrue(COMPOSE.is_file())

    def test_preflight_documents_provider_matrix(self):
        text = PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("CLOUD_PROVIDER", text)
        self.assertIn("bedrock embeddings require CLOUD_PROVIDER=aws", text)
        for provider in CROSS_CLOUD_PROVIDERS:
            self.assertIn(provider, text)

    def test_embed_sh_blocks_bedrock_off_aws(self):
        text = EMBED.read_text(encoding="utf-8")
        self.assertIn('EMBEDDINGS_PROVIDER" == "bedrock"', text)
        self.assertIn("CLOUD_PROVIDER", text)

    def test_compose_passes_cloud_provider(self):
        compose = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("CLOUD_PROVIDER:", compose)
        self.assertIn("${CLOUD_PROVIDER:-aws}", compose)

    def test_local_still_blocked_for_memory_api_multi_agent(self):
        text = MEMORY_PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("local embeddings provider is not supported by memory-api", text)

    def test_embed_sh_rejects_bedrock_on_gcp(self):
        with tempfile.TemporaryDirectory():
            env = {
                **os.environ,
                "EMBEDDINGS_PROVIDER": "bedrock",
                "EMBEDDINGS_MODEL": "amazon.titan-embed-text-v2:0",
                "EMBEDDINGS_DIMENSIONS": "1024",
                "CLOUD_PROVIDER": "gcp",
                "PATH": os.environ.get("PATH", ""),
            }
            result = subprocess.run(
                ["bash", str(EMBED), "hello"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(
                (result.stderr + result.stdout).lower(),
                r"bedrock.*aws|cloud_provider=aws",
            )

    def test_embed_sh_allows_openai_on_gcp_with_fake_curl(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            # Minimal openai embeddings response.
            curl = bin_dir / "curl"
            curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    echo '{"data":[{"embedding":[0.1,0.2,0.3]}]}'
                    """
                ),
                encoding="utf-8",
            )
            curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
            jq = bin_dir / "jq"
            # Prefer real jq if present; otherwise tiny stub.
            if not any(
                Path(p).joinpath("jq").exists()
                for p in os.environ.get("PATH", "").split(":")
                if p
            ):
                jq.write_text(
                    textwrap.dedent(
                        """\
                        #!/usr/bin/env bash
                        # Very small stub: -c '.data[0].embedding'
                        python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["embedding"])'
                        """
                    ),
                    encoding="utf-8",
                )
                jq.chmod(jq.stat().st_mode | stat.S_IXUSR)
                path = f"{bin_dir}:{os.environ.get('PATH', '')}"
            else:
                path = f"{bin_dir}:{os.environ.get('PATH', '')}"
            env = {
                **os.environ,
                "PATH": path,
                "EMBEDDINGS_PROVIDER": "openai",
                "EMBEDDINGS_MODEL": "text-embedding-3-small",
                "EMBEDDINGS_DIMENSIONS": "3",
                "CLOUD_PROVIDER": "gcp",
                "OPENAI_API_KEY": "sk-test",
            }
            result = subprocess.run(
                ["bash", str(EMBED), "hello"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("::vector(3)", result.stdout)

    def test_preflight_rejects_bedrock_on_gcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            setup = data_dir / ".setup"
            setup.mkdir(parents=True)
            secrets = setup / "secrets.env"
            secrets.write_text(
                "EMBEDDINGS_PROVIDER=bedrock\nCLOUD_PROVIDER=gcp\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "DATA_DIR": str(data_dir),
                "ENTERPRISE_ASSETS_DIR": str(REPO_ROOT),
                "AGENT_RUNTIME_VERSION": "test-version",
                "CLOUD_PROVIDER": "gcp",
                "EMBEDDINGS_PROVIDER": "bedrock",
            }
            result = subprocess.run(
                ["bash", str(PREFLIGHT)],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            combined = (result.stderr + result.stdout).lower()
            self.assertIn("bedrock", combined)
            self.assertIn("aws", combined)


if __name__ == "__main__":
    unittest.main()
