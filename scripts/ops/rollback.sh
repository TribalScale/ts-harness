#!/usr/bin/env bash
# Move this host to a released version (forward or back) and redeploy.
#
#   sudo ADMIN_USER=<admin-user> bash scripts/ops/rollback.sh v0.2.0
#
# Checks out the tag in enterprise-assets (detached) and runs redeploy.sh, which waits for running
# coding jobs to finish first. Database migrations only move forward and are additive (a contract
# test refuses destructive ones), so an older release runs on the newer schema; nothing is rolled
# back in the database. To return to the configured branch afterwards, check it out and redeploy.
set -euo pipefail

TAG="${1:-}"
if [[ -z "$TAG" || ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "usage: rollback.sh v<major>.<minor>.<patch>" >&2
    exit 2
fi
if [[ -z "${ADMIN_USER:-}" ]]; then
    echo "ERROR: set ADMIN_USER (the host's admin account, which owns enterprise-assets)" >&2
    exit 1
fi
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$OPS_DIR/../.." && pwd)"

sudo -u "$ADMIN_USER" git -C "$REPO_DIR" fetch --tags origin
if ! sudo -u "$ADMIN_USER" git -C "$REPO_DIR" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "ERROR: tag $TAG not found" >&2
    exit 1
fi
echo "--- moving $(sudo -u "$ADMIN_USER" git -C "$REPO_DIR" log --oneline -1) -> $TAG"
sudo -u "$ADMIN_USER" git -C "$REPO_DIR" checkout -q --detach "$TAG"
exec bash "$OPS_DIR/redeploy.sh"
