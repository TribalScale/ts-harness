#!/usr/bin/env bash
# Run TS Harness locally with Docker: no cloud account, fake model provider by default.
#
#   scripts/quickstart.sh up      build and start the stack, print the console URL and token
#   scripts/quickstart.sh down    stop the stack (add --volumes to delete its data)
#   scripts/quickstart.sh logs    follow agent-runtime logs
#   scripts/quickstart.sh token   print the gateway token
#   scripts/quickstart.sh gates   run the CI release gates inside the agent-runtime container
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${REPO_ROOT}/.quickstart"
ENV_FILE="${STATE_DIR}/.env"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "${REPO_ROOT}/docker/docker-compose.quickstart.yml")

random_token() {
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

ensure_env() {
    mkdir -p "$STATE_DIR"
    if [[ ! -f "$ENV_FILE" ]]; then
        umask 077
        {
            echo "GATEWAY_TOKEN=$(random_token)"
            echo "MEMORY_API_TOKEN=$(random_token)"
            echo "SANDBOX_WORKER_TOKEN=$(random_token)"
            echo "QUICKSTART_DB_PASSWORD=$(random_token)"
            echo "# Set RUNTIME_FAKE_PROVIDER=false and a provider key to use a real model."
            echo "RUNTIME_FAKE_PROVIDER=true"
            echo "OPENAI_API_KEY="
            echo "ANTHROPIC_API_KEY="
        } > "$ENV_FILE"
        echo "Wrote ${ENV_FILE}"
    fi
}

env_value() {
    { grep -E "^$1=" "$ENV_FILE" || true; } | tail -n 1 | cut -d= -f2-
}

wait_healthy() {
    local port url
    port="$(env_value AGENT_RUNTIME_PORT)"
    url="http://127.0.0.1:${port:-18789}/healthz"
    for _ in $(seq 1 90); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "agent-runtime did not become healthy; check: scripts/quickstart.sh logs" >&2
    return 1
}

cmd="${1:-up}"
shift || true

case "$cmd" in
    up)
        command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
        ensure_env
        "${COMPOSE[@]}" up -d --build "$@"
        wait_healthy
        ui_port="$(env_value ADMIN_UI_PORT)"
        echo
        echo "TS Harness is running."
        echo "  Admin console: http://localhost:${ui_port:-8080}"
        echo "  Gateway token: $(env_value GATEWAY_TOKEN)"
        ;;
    down)
        [[ -f "$ENV_FILE" ]] || { echo "Nothing to stop."; exit 0; }
        "${COMPOSE[@]}" down "$@"
        ;;
    logs)
        "${COMPOSE[@]}" logs -f agent-runtime "$@"
        ;;
    token)
        env_value GATEWAY_TOKEN
        ;;
    gates)
        "${COMPOSE[@]}" exec -T \
            -e RELEASE_GATES_PROFILE="${RELEASE_GATES_PROFILE:-ci}" \
            -e AGENT_RUNTIME_URL=http://127.0.0.1:18789 \
            -e TS_HARNESS_CONFIG=/tmp/ts-harness-runtime.yaml \
            agent-runtime python3 /app/scripts/release-gates.py
        ;;
    *)
        echo "usage: $0 {up|down|logs|token|gates}" >&2
        exit 2
        ;;
esac
