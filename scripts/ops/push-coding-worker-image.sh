#!/usr/bin/env bash
# Push the coding worker image this host just built to ECR, so Fargate workers run the same
# harness code as the host. redeploy.sh calls it after the image build; run it by hand after
# the first Terraform apply that turns on ECS workers:
#
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/push-coding-worker-image.sh
#
# It does nothing when the runtime config names no ECR repository (no Fargate stack).
# Tags pushed: CODING_WORKER_ECR_TAG (what the task definition pulls, default latest) and the
# git short SHA (for rollback: set coding_worker_image_tag to it and apply).
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
PHASES_DIR="$SCRIPT_DIR/phases"
DATA_DIR="${DATA_DIR:-/vol/ts-harness-data}"
ENTERPRISE_ASSETS_DIR="${ENTERPRISE_ASSETS_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export DATA_DIR ENTERPRISE_ASSETS_DIR SCRIPT_DIR

# shellcheck disable=SC1091
source "$PHASES_DIR/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"

load_instance_env
resolve_cloud_neutral_env

repository="${CODING_WORKER_ECR_REPOSITORY:-}"
if is_blank_or_placeholder "$repository"; then
    echo "[push-coding-worker-image] No ECR repository in the runtime config; nothing to push."
    exit 0
fi
region="${CLOUD_REGION:-${AWS_REGION:-}}"
source_image="${CODING_WORKER_IMAGE:-ts-harness-coding-worker:latest}"
tag="${CODING_WORKER_ECR_TAG:-latest}"
sha="$(git -C "$ENTERPRISE_ASSETS_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
registry="${repository%%/*}"

if ! docker image inspect "$source_image" >/dev/null 2>&1; then
    echo "[push-coding-worker-image] ERROR: $source_image is not built on this host (run the redeploy first)" >&2
    exit 1
fi

# A throwaway Docker config keeps the 12-hour ECR login off the host's root config.
DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
aws ecr get-login-password --region "$region" \
    | docker login --username AWS --password-stdin "$registry" >/dev/null

for t in "$tag" "$sha"; do
    docker tag "$source_image" "$repository:$t"
    echo "[push-coding-worker-image] Pushing $repository:$t"
    docker push --quiet "$repository:$t" >/dev/null
    [[ "$t" == "$tag" ]] || docker rmi "$repository:$t" >/dev/null 2>&1 || true
done
echo "[push-coding-worker-image] Workers started from now on pull $repository:$tag ($sha)."
