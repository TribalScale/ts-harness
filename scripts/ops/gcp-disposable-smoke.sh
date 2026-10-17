#!/usr/bin/env bash
# Disposable GCP install smoke: validate → plan → apply → secret note →
# wait/hooks → health checks → destroy.
#
# SAFETY: refuses to run real cloud mutations unless GCP_SMOKE_CONFIRM=1.
# Prefer --dry-run first. Does not require live credentials for dry-run.
#
# Required env (live run):
#   GCP_SMOKE_CONFIRM=1
#   GCP_PROJECT_ID          — disposable GCP project
#   INSTALL_CONFIG          — path to install YAML (cloud_provider: gcp)
# Optional:
#   CLOUD_REGION / CLOUD_ZONE
#   SMOKE_SKIP_DESTROY=1    — leave infra up after health checks
#   SMOKE_WAIT_SECONDS      — bootstrap wait (default 900)
#   GATEWAY_PORT            — default 18789
#   HEALTH_BASE_URL         — override health base (default via gcloud/SSH)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTALL_SH="${REPO_ROOT}/scripts/install.sh"
TF_ROOT="${REPO_ROOT}/infra/gcp"
DRY_RUN=false
SKIP_DESTROY="${SMOKE_SKIP_DESTROY:-0}"
WAIT_SECONDS="${SMOKE_WAIT_SECONDS:-900}"
GATEWAY_PORT="${GATEWAY_PORT:-18789}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--dry-run] [--help]

Orchestrates a disposable GCP TS Harness smoke lifecycle:
  validate → plan → apply → secret placeholders note → wait hooks →
  health checks → destroy

Flags:
  --dry-run   Print planned steps and installer dry-run commands only
              (no apply/destroy; does not need GCP_SMOKE_CONFIRM)

Environment:
  GCP_SMOKE_CONFIRM=1   Required for any live apply/destroy (safe default: refuse)
  GCP_PROJECT_ID        Disposable project id (live run)
  INSTALL_CONFIG        Install contract YAML with cloud_provider: gcp
  SMOKE_SKIP_DESTROY=1  Keep resources after health checks
  SMOKE_WAIT_SECONDS    Seconds to wait for bootstrap (default 900)

Examples:
  # Safe local rehearsal (no credentials required):
  INSTALL_CONFIG=config/install/gcp.example.yaml \\
    $0 --dry-run

  # Live disposable project (destructive; requires confirm):
  export GCP_SMOKE_CONFIRM=1
  export GCP_PROJECT_ID=my-disposable-project
  export INSTALL_CONFIG=/path/to/gcp-disposable.yaml
  $0
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "[gcp-smoke] $*"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

INSTALL_CONFIG="${INSTALL_CONFIG:-}"
[[ -n "$INSTALL_CONFIG" ]] || die "INSTALL_CONFIG is required (path to gcp install YAML)"
[[ -f "$INSTALL_CONFIG" ]] || die "INSTALL_CONFIG not found: $INSTALL_CONFIG"
[[ -x "$INSTALL_SH" || -f "$INSTALL_SH" ]] || die "installer missing: $INSTALL_SH"
[[ -d "$TF_ROOT" ]] || die "GCP terraform root missing: $TF_ROOT"

if [[ "$DRY_RUN" != "true" && "${GCP_SMOKE_CONFIRM:-}" != "1" ]]; then
  die "Refusing live GCP smoke without GCP_SMOKE_CONFIRM=1 (pass --dry-run to rehearse)"
fi

if [[ "$DRY_RUN" != "true" ]]; then
  [[ -n "${GCP_PROJECT_ID:-}" ]] || die "GCP_PROJECT_ID is required for live smoke"
  command -v gcloud >/dev/null 2>&1 || die "gcloud is required for live smoke"
  command -v terraform >/dev/null 2>&1 || die "terraform is required for live smoke"
fi

run_install() {
  local cmd="$1"
  shift
  if [[ "$DRY_RUN" == "true" ]]; then
    log "DRY-RUN: ./scripts/install.sh $cmd --cloud gcp --config $INSTALL_CONFIG --dry-run $*"
    "$INSTALL_SH" "$cmd" --cloud gcp --config "$INSTALL_CONFIG" --dry-run "$@"
  else
    log "Running: ./scripts/install.sh $cmd --cloud gcp --config $INSTALL_CONFIG $*"
    "$INSTALL_SH" "$cmd" --cloud gcp --config "$INSTALL_CONFIG" "$@"
  fi
}

log "Step 1/7: validate"
run_install validate

log "Step 2/7: plan"
run_install plan

if [[ "$DRY_RUN" == "true" ]]; then
  log "Step 3/7: apply (skipped — dry-run)"
  log "Step 4/7: secret placeholders"
  log "  After a live apply, populate Secret Manager placeholders (logical keys):"
  log "    anthropic-api-key, gateway-token, and optional connectors"
  log "  Terraform seeds placeholder values; operators must replace them before bootstrap completes."
  log "Step 5/7: wait for bootstrap hooks (skipped — dry-run)"
  log "Step 6/7: health checks (skipped — dry-run)"
  log "  Would check: GET /healthz (port ${GATEWAY_PORT}), memory-api /health (18792),"
  log "  and scripts/ops/post-deploy-smoke.sh when SSH/tunnel is available."
  log "Step 7/7: destroy (skipped — dry-run)"
  log "Dry-run complete. Set GCP_SMOKE_CONFIRM=1 and omit --dry-run for a live disposable run."
  exit 0
fi

log "Step 3/7: apply"
run_install apply

log "Step 4/7: secret placeholders note"
cat <<EOF
[gcp-smoke] Populate Secret Manager placeholders for logical keys (prefix from install config),
including at least:
  - anthropic-api-key
  - gateway-token
Optional connectors (Slack, Jira, Google Workspace) may remain unset for reduced-capability smoke.
Managed DB credentials are written by Terraform/Cloud SQL and fetched via MANAGED_DB_CREDENTIAL_REF.
EOF

log "Step 5/7: wait for bootstrap hooks (up to ${WAIT_SECONDS}s)"
# Best-effort: poll operator-facing completion via gcloud SSH when instance is known.
deadline=$((SECONDS + WAIT_SECONDS))
ready=false
while (( SECONDS < deadline )); do
  if [[ -n "${HEALTH_BASE_URL:-}" ]]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
      "${HEALTH_BASE_URL%/}/healthz" || echo 000)"
    if [[ "$code" == "200" ]]; then
      ready=true
      break
    fi
  else
    log "  waiting… set HEALTH_BASE_URL=http://<host>:${GATEWAY_PORT} to poll /healthz"
  fi
  sleep 30
done
if [[ "$ready" != "true" && -z "${HEALTH_BASE_URL:-}" ]]; then
  log "WARNING: HEALTH_BASE_URL unset; skipping automated wait. Continue with manual checks."
elif [[ "$ready" != "true" ]]; then
  die "Bootstrap health wait timed out after ${WAIT_SECONDS}s"
fi

log "Step 6/7: health checks"
if [[ -n "${HEALTH_BASE_URL:-}" ]]; then
  export AGENT_RUNTIME_URL="${HEALTH_BASE_URL}"
  bash "${REPO_ROOT}/scripts/ops/post-deploy-smoke.sh" || die "post-deploy smoke failed"
else
  log "WARNING: HEALTH_BASE_URL unset; run scripts/ops/post-deploy-smoke.sh over SSH manually."
fi

if [[ "$SKIP_DESTROY" == "1" ]]; then
  log "Step 7/7: destroy skipped (SMOKE_SKIP_DESTROY=1)"
  log "Remember to destroy the disposable project resources when finished."
  exit 0
fi

log "Step 7/7: destroy"
env_name="$(
  INSTALL_CONFIG="$INSTALL_CONFIG" python3 - <<'PY'
import os, yaml
cfg = yaml.safe_load(open(os.environ["INSTALL_CONFIG"]))
print(cfg.get("environment") or "")
PY
)"
[[ -n "$env_name" ]] || die "Could not read environment from INSTALL_CONFIG"
"$INSTALL_SH" destroy --cloud gcp --config "$INSTALL_CONFIG" --confirm "$env_name"

log "Disposable GCP smoke finished (validate→plan→apply→health→destroy)."
log "Verify no retained chargeable resources except intentionally retained state/bucket."
