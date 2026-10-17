import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker" / "docker-compose.memory-api.yml"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.memory-api"
COMPOSE_PHASE = REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh"
COMPOSE_COMMON = REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh"
ENV_DEFAULTS = REPO_ROOT / "scripts" / "phases" / "lib" / "env-defaults.sh"


class MemoryApiComposeTests(unittest.TestCase):
    def test_memory_api_uses_repo_owned_image_build(self):
        compose = COMPOSE.read_text()

        self.assertNotIn("image: python:3.12-slim", compose)
        self.assertIn("build:", compose)
        self.assertIn(
            "context: ${ENTERPRISE_ASSETS_DIR:?set ENTERPRISE_ASSETS_DIR}",
            compose,
        )
        self.assertIn("dockerfile: docker/Dockerfile.memory-api", compose)
        self.assertIn(
            'command: ["python3", "/app/memory-gateway.py"]',
            compose,
        )

    def test_memory_api_mounts_enterprise_scripts_not_relative_scripts(self):
        compose = COMPOSE.read_text()

        self.assertNotIn("- ./scripts:/scripts:ro", compose)
        self.assertIn(
            "${ENTERPRISE_ASSETS_DIR:?set ENTERPRISE_ASSETS_DIR}/scripts:/scripts:ro",
            compose,
        )
        self.assertIn(
            "${TS_HARNESS_CONFIG_DIR:?set TS_HARNESS_CONFIG_DIR}:/config:ro",
            compose,
        )

    def test_memory_api_passes_db_and_embedding_environment(self):
        compose = COMPOSE.read_text()

        for name in (
            "TS_HARNESS_DB_HOST",
            "TS_HARNESS_DB_PORT",
            "TS_HARNESS_DB_USER",
            "TS_HARNESS_DB_NAME",
            "TS_HARNESS_DB_PASSWORD",
            "EMBEDDINGS_PROVIDER",
            "EMBEDDINGS_MODEL",
            "EMBEDDINGS_DIMENSIONS",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "OPENAI_API_KEY",
            "VOYAGE_API_KEY",
            "GEMINI_API_KEY",
            "MISTRAL_API_KEY",
        ):
            self.assertIn(f"{name}:", compose)

    def test_memory_api_dockerfile_contains_runtime_dependencies(self):
        dockerfile = DOCKERFILE.read_text()

        for package in ("bash", "curl", "jq", "ca-certificates", "awscli"):
            self.assertIn(package, dockerfile)
        self.assertIn("psycopg[binary]", dockerfile)
        self.assertIn("psycopg-pool", dockerfile)
        self.assertIn(
            "COPY scripts/memory-gateway.py /app/memory-gateway.py",
            dockerfile,
        )
        self.assertIn("COPY scripts/embed.sh /scripts/embed.sh", dockerfile)

    def test_local_embeddings_are_blocked_for_memory_api_sidecar(self):
        preflight = (REPO_ROOT / "scripts" / "preflight-memory-env.sh").read_text()

        self.assertIn("local embeddings provider is not supported by memory-api", preflight)

    def test_compose_phase_exports_enterprise_assets_and_embedding_keys(self):
        phase = COMPOSE_PHASE.read_text()
        common = COMPOSE_COMMON.read_text()
        env_defaults = ENV_DEFAULTS.read_text()
        surface = "\n".join((phase, common, env_defaults))

        for name in (
            "ENTERPRISE_ASSETS_DIR",
            "AWS_REGION",
            "CLOUD_REGION",
            "OPENAI_API_KEY",
            "VOYAGE_API_KEY",
            "GEMINI_API_KEY",
            "MISTRAL_API_KEY",
        ):
            self.assertIn(f"{name}=", surface)
        phase = COMPOSE_PHASE.read_text()
        self.assertIn("docker-compose.local-db.yml", phase)
        self.assertIn("docker-compose.managed-db.yml", phase)
        self.assertNotIn("docker-compose.override.yml", phase)
        self.assertNotIn("docker-compose.rds.yml", phase)


if __name__ == "__main__":
    unittest.main()
