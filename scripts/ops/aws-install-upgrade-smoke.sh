#!/usr/bin/env bash
# AWS install upgrade rehearsal: wrap migrate-aws-terraform-state.sh and
# re-check the saved plan for critical destroy/replace actions.
#
# SAFETY: refuses unless AWS_UPGRADE_CONFIRM=1.
# Prefer --dry-run to print the rehearsal steps without touching state.
#
# Required env (live run):
#   AWS_UPGRADE_CONFIRM=1
# Optional (forwarded to migrate script):
#   EXPECTED_ACCOUNT_ID
#   EXPECTED_REGION
#   BACKUP_DIR
#   PLAN_FILE
#   OLD_ROOT / NEW_ROOT
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MIGRATE_SH="${REPO_ROOT}/scripts/migrate-aws-terraform-state.sh"
NEW_ROOT="${NEW_ROOT:-$REPO_ROOT/infra/aws}"
PLAN_FILE="${PLAN_FILE:-$NEW_ROOT/migration.tfplan}"
DRY_RUN=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [--dry-run] [--help]

Rehearses an existing AWS install upgrade against infra/aws:
  1. Runs scripts/migrate-aws-terraform-state.sh (backup, validate, plan)
  2. Re-checks the plan JSON for refuse-destroy of VM/disk/RDS/secrets/KMS

Flags:
  --dry-run   Print steps only (no terraform/aws calls; no AWS_UPGRADE_CONFIRM)

Environment:
  AWS_UPGRADE_CONFIRM=1   Required for live rehearsal (safe default: refuse)
  EXPECTED_ACCOUNT_ID     Optional account gate for migrate script
  EXPECTED_REGION         Optional region gate for migrate script
  PLAN_FILE               Override plan path (default: infra/aws/migration.tfplan)

See docs/runbooks/aws-terraform-root-migration.md and docs/runbooks/aws-install.md.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "[aws-upgrade-smoke] $*"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

[[ -f "$MIGRATE_SH" ]] || die "migrate script missing: $MIGRATE_SH"
[[ -d "$NEW_ROOT" ]] || die "AWS terraform root missing: $NEW_ROOT"

if [[ "$DRY_RUN" != "true" && "${AWS_UPGRADE_CONFIRM:-}" != "1" ]]; then
  die "Refusing AWS upgrade rehearsal without AWS_UPGRADE_CONFIRM=1 (pass --dry-run)"
fi

if [[ "$DRY_RUN" == "true" ]]; then
  log "DRY-RUN steps:"
  log "  1. export EXPECTED_ACCOUNT_ID / EXPECTED_REGION (recommended)"
  log "  2. $MIGRATE_SH"
  log "  3. terraform show -json $PLAN_FILE | refuse-destroy check"
  log "Critical addresses refused on destroy/replace:"
  log "  aws_instance.harness"
  log "  aws_ebs_volume.harness_data / aws_volume_attachment.harness_data"
  log "  aws_db_instance.harness"
  log "  aws_secretsmanager_secret.harness"
  log "  aws_kms_key.agent_credentials / aws_kms_alias.agent_credentials"
  log "Dry-run complete. Set AWS_UPGRADE_CONFIRM=1 and omit --dry-run for live rehearsal."
  exit 0
fi

command -v terraform >/dev/null 2>&1 || die "terraform is required"
command -v aws >/dev/null 2>&1 || die "aws CLI is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

log "Step 1/2: migrate-aws-terraform-state.sh"
bash "$MIGRATE_SH"

[[ -f "$PLAN_FILE" ]] || die "Plan file missing after migrate: $PLAN_FILE"

log "Step 2/2: refuse-destroy re-check on $PLAN_FILE"
(
  cd "$NEW_ROOT"
  terraform show -json "$PLAN_FILE"
) | python3 -c '
import json, sys

critical = [
    "aws_instance.harness",
    "aws_ebs_volume.harness_data",
    "aws_volume_attachment.harness_data",
    "aws_db_instance.harness",
    "aws_secretsmanager_secret.harness",
    "aws_kms_key.agent_credentials",
    "aws_kms_alias.agent_credentials",
]
plan = json.load(sys.stdin)
bad = []
for rc in plan.get("resource_changes") or []:
    addr = rc.get("address") or ""
    actions = set((rc.get("change") or {}).get("actions") or [])
    if not actions.intersection({"delete", "replace"}):
        continue
    for needle in critical:
        if addr == needle or addr.startswith(needle + "["):
            bad.append(f"{addr}: {sorted(actions)}")
            break
if bad:
    print("CRITICAL DESTROY/REPLACE DETECTED:", file=sys.stderr)
    for line in bad:
        print(f"  - {line}", file=sys.stderr)
    sys.exit(3)
print("No critical destroy/replace actions detected.")
'

log "AWS upgrade rehearsal succeeded."
log "  Plan: $PLAN_FILE"
log "Next: review the plan, apply only from infra/aws/, then run post-deploy smoke."
log "Acceptance: zero-destroy of protected resources, bootstrap with AWS adapters,"
log "unchanged secret names, DB continuity, /healthz + memory smoke."
