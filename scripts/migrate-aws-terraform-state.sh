#!/usr/bin/env bash
# Migrate an existing AWS Terraform root from terraform/ to infra/aws/ while
# preserving resource addresses. Backs up state, validates account/region,
# runs plan with detailed-exitcode, and refuses destroy of critical resources.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OLD_ROOT="${OLD_ROOT:-$REPO_ROOT/terraform}"
NEW_ROOT="${NEW_ROOT:-$REPO_ROOT/infra/aws}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/.terraform-migration-backups}"
EXPECTED_ACCOUNT_ID="${EXPECTED_ACCOUNT_ID:-}"
EXPECTED_REGION="${EXPECTED_REGION:-}"
PLAN_FILE="${PLAN_FILE:-$NEW_ROOT/migration.tfplan}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

require_cmd terraform
require_cmd aws
require_cmd python3

[[ -d "$NEW_ROOT" ]] || die "new AWS root missing: $NEW_ROOT"
[[ -f "$NEW_ROOT/main.tf" ]] || die "new AWS root incomplete: $NEW_ROOT/main.tf"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
backup_path="$BACKUP_DIR/aws-tfstate-$timestamp"

echo "==> Validating caller identity"
caller_json="$(aws sts get-caller-identity --output json)"
caller_account="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Account"])' <<<"$caller_json")"
caller_arn="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])' <<<"$caller_json")"
echo "    Account: $caller_account"
echo "    Arn:     $caller_arn"

if [[ -n "$EXPECTED_ACCOUNT_ID" && "$caller_account" != "$EXPECTED_ACCOUNT_ID" ]]; then
  die "AWS account mismatch: expected $EXPECTED_ACCOUNT_ID, got $caller_account"
fi

configured_region="$(aws configure get region 2>/dev/null || true)"
effective_region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$configured_region}}"
if [[ -n "$EXPECTED_REGION" ]]; then
  [[ -n "$effective_region" ]] || die "EXPECTED_REGION=$EXPECTED_REGION set but no AWS region configured"
  [[ "$effective_region" == "$EXPECTED_REGION" ]] || die \
    "AWS region mismatch: expected $EXPECTED_REGION, got $effective_region"
fi
echo "    Region:  ${effective_region:-<unset>}"

echo "==> Backing up state pull to $backup_path"
mkdir -p "$backup_path"
(
  cd "$NEW_ROOT"
  if [[ ! -d .terraform ]]; then
    echo "    Initializing Terraform in $NEW_ROOT"
    terraform init
  fi
  terraform state pull >"$backup_path/terraform.tfstate"
)
cp -a "$NEW_ROOT"/*.tf "$backup_path/" 2>/dev/null || true
if [[ -f "$NEW_ROOT/backend.tf" ]]; then
  cp "$NEW_ROOT/backend.tf" "$backup_path/backend.tf"
fi
echo "    State backup written: $backup_path/terraform.tfstate"

echo "==> Running terraform validate"
(
  cd "$NEW_ROOT"
  terraform validate
)

echo "==> Running terraform plan -detailed-exitcode"
set +e
(
  cd "$NEW_ROOT"
  terraform plan -detailed-exitcode -out="$PLAN_FILE"
)
plan_rc=$?
set -e

case "$plan_rc" in
  0)
    echo "Plan OK: no changes."
    ;;
  2)
    echo "Plan OK: changes present (exit code 2)."
    ;;
  *)
    die "terraform plan failed with exit code $plan_rc"
    ;;
esac

echo "==> Checking plan for critical destroys"
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

echo
echo "Migration rehearsal succeeded."
echo "  Backup: $backup_path"
echo "  Plan:   $PLAN_FILE"
echo "  Old root (compat only): $OLD_ROOT"
echo "  Active root:            $NEW_ROOT"
echo
echo "Next: review the plan, then apply from $NEW_ROOT only."
echo "Do not maintain two active AWS roots."
