#!/usr/bin/env bash
# migrate-host-state.sh - run migrate-host-state.py next to the running agent-runtime.
#
# Starts a one-off container from the agent-runtime image that shares its
# network namespace (so 127.0.0.1:18792 is memory-api) and its mounts in
# read-write mode (the old volumes are read-only once a backend is switched,
# and --export writes to them). The environment is copied from agent-runtime.
#
# Usage (as root on the host):
#   sudo bash scripts/ops/migrate-host-state.sh --import --object-backend s3 \
#       --skills-bucket NAME --workspace-bucket NAME
#   sudo bash scripts/ops/migrate-host-state.sh --verify ...
#   sudo bash scripts/ops/migrate-host-state.sh --export ...
#
# Options:
#   RUNTIME_CONTAINER  container to attach to (default: the compose agent-runtime service)
# See docs/runbooks/shared-state.md.
set -euo pipefail

SCRIPT_PATH="/app/scripts/ops/migrate-host-state.py"

if [[ $# -eq 0 ]]; then
    echo "usage: $0 --import|--verify|--export [migrate-host-state.py options]" >&2
    exit 2
fi

CID="${RUNTIME_CONTAINER:-}"
if [[ -z "$CID" ]]; then
    CID="$(docker ps -q --filter label=com.docker.compose.service=agent-runtime | head -n 1)"
fi
if [[ -z "$CID" ]]; then
    echo "migrate-host-state: agent-runtime is not running; start it (phase 12) and retry" >&2
    exit 1
fi
IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CID")"

ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
chmod 600 "$ENV_FILE"
docker exec "$CID" env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' | grep -vE '^(HOSTNAME|HOME|PATH)=' > "$ENV_FILE"

if ! docker exec "$CID" test -f "$SCRIPT_PATH"; then
    echo "migrate-host-state: $SCRIPT_PATH is not in the running image; deploy this release first" >&2
    exit 1
fi

# Not exec: the EXIT trap must remove the copied environment.
docker run --rm -i \
    --network "container:${CID}" \
    --volumes-from "${CID}:rw" \
    --env-file "$ENV_FILE" \
    --label ts-harness.migrate-host-state=true \
    "$IMAGE" python3 "$SCRIPT_PATH" "$@"
