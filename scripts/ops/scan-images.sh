#!/usr/bin/env bash
# Scan the local agent-runtime image for HIGH and CRITICAL vulnerabilities.
# Local machines without trivy exit 0. CI uses aquasecurity/trivy-action and fails on findings.
set -euo pipefail

IMAGE="${1:-ts-harness-agent-runtime:latest}"

if ! command -v trivy >/dev/null 2>&1; then
  echo "trivy is not installed; skipping image scan of ${IMAGE}"
  exit 0
fi

exec trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed "${IMAGE}"
