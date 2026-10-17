from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bootstrap_runs_in_repo_agent_runtime_setup():
    user_data = (REPO_ROOT / "scripts" / "user-data.sh").read_text()
    phases = (REPO_ROOT / "scripts" / "bootstrap" / "phases.sh").read_text()

    assert "08-clone-upstream.sh" not in user_data
    assert "08-clone-upstream.sh" not in phases
    assert "bootstrap/run.sh" in user_data
    assert "11-agent-runtime-setup.sh" in phases
    assert phases.index("16-release-gates.sh") < phases.index("99-finalize.sh")


def test_compose_uses_ts_harness_assets():
    compose_phase = (REPO_ROOT / "scripts" / "phases" / "12-compose-up.sh").read_text()

    assert 'COMPOSE_FILES=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.memory-api.yml")' in compose_phase
    assert 'cd "$ENTERPRISE_ASSETS_DIR"' in compose_phase
    assert "docker-compose.agent-runtime.yml" in compose_phase


def test_ops_scripts_target_agent_runtime():
    for rel in (
        "scripts/ops/verify-multi-agent.sh",
        "scripts/ops/rotate-secrets.sh",
        "scripts/verify-memory-stack.sh",
    ):
        text = (REPO_ROOT / rel).read_text()
        assert "agent-runtime" in text, rel


def test_agent_runtime_compose_uses_shared_memory_namespace_and_runtime_env_file():
    compose = (REPO_ROOT / "docker" / "docker-compose.agent-runtime.yml").read_text()

    assert "MEMORY_API_URL: ${MEMORY_API_URL:-http://127.0.0.1:18792}" in compose
    assert "env_file:" in compose
    assert "SLACK_BOT_TOKEN:" in compose
    assert "DISCORD_WEBHOOK_URL:" in compose
    assert "TELEGRAM_BOT_TOKEN:" in compose
    assert "OPENAI_BASE_URL:" in compose
    assert "ANTHROPIC_BASE_URL:" in compose
    assert "MODEL_API_KEY:" in compose


def test_agents_init_uses_ts_harness_config_dir():
    phase = (REPO_ROOT / "scripts" / "phases" / "11a-agents-init.sh").read_text()

    assert "TS_HARNESS_CONFIG_DIR" in phase
    assert "ts-harness-runtime.yaml.tmpl" in phase


def test_runtime_image_provides_mcporter_command():
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile.agent-runtime").read_text()

    assert "COPY scripts/mcporter" in dockerfile
    assert "/usr/local/bin/mcporter" in dockerfile
