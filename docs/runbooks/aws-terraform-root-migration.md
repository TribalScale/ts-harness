# AWS Terraform Root Migration (`terraform/` → `infra/aws/`)

## Summary

The live AWS Terraform root moved from `terraform/` to `infra/aws/`. Resource
addresses are unchanged; migration is a **working-directory cutover** against
the same remote state backend. `terraform/README.md` is a compatibility pointer
only — do not apply from both roots.

## Prerequisites

- Terraform `>= 1.5`
- AWS credentials for the target account/region
- Copy of `infra/aws/backend.tf` (from `backend.tf` / example) pointing at the
  existing state bucket/key
- Non-production rehearsal of real state before production cutover

Optional environment gates for the migration script:

| Variable | Purpose |
|---|---|
| `EXPECTED_ACCOUNT_ID` | Refuse if `sts get-caller-identity` differs |
| `EXPECTED_REGION` | Refuse if configured AWS region differs |
| `BACKUP_DIR` | Override state backup directory |

Operator wrapper (refuse-destroy re-check + confirm gate):

```bash
# Safe print-only rehearsal
./scripts/ops/aws-install-upgrade-smoke.sh --dry-run

# Live non-production rehearsal
export AWS_UPGRADE_CONFIRM=1
export EXPECTED_ACCOUNT_ID=123456789012
export EXPECTED_REGION=us-east-1
./scripts/ops/aws-install-upgrade-smoke.sh
```

See also [`aws-install.md`](aws-install.md).

## Cutover steps

1. Confirm no one is applying from the old `terraform/` directory.
2. Ensure `infra/aws/backend.tf` matches the existing remote state location.
3. From a clean checkout on the migrated branch:

   ```bash
   export EXPECTED_ACCOUNT_ID=123456789012
   export EXPECTED_REGION=us-east-1
   ./scripts/migrate-aws-terraform-state.sh
   ```

4. The script will:
   - Validate AWS account/region
   - `terraform state pull` backup under `.terraform-migration-backups/`
   - `terraform validate`
   - `terraform plan -detailed-exitcode`
   - Refuse destroy/replace of VM, data disk, RDS, secrets, or KMS
5. Review the saved plan (`infra/aws/migration.tfplan`).
6. Apply only from the new root:

   ```bash
   cd infra/aws
   terraform apply migration.tfplan
   ```

7. Update operator docs/scripts to use `infra/aws/` going forward.

## State address policy

- Prefer keeping resource addresses identical so no `moved` blocks or
  `terraform state mv` are required.
- If an address must change, add an explicit `moved` block in
  `infra/aws/migrations.tf` and rehearse again before production.
- Never keep two active AWS roots after cutover.

## Acceptance

- Migration script exits 0 (plan may be no-change or non-critical changes).
- No destroy/replace of:
  - `aws_instance.harness`
  - `aws_ebs_volume.harness_data` / `aws_volume_attachment.harness_data`
  - `aws_db_instance.harness`
  - `aws_secretsmanager_secret.harness`
  - `aws_kms_key.agent_credentials` / `aws_kms_alias.agent_credentials`
- Bootstrap continues with AWS adapters, unchanged secret names, and DB continuity.
- Post-deploy smoke (`/healthz`, memory health, workflow tick as applicable) passes.

## Rollback

1. Stop applies from `infra/aws/`.
2. Restore the backed-up state JSON if the remote state was corrupted
   (`terraform state push` only with extreme care and leadership approval).
3. Check out the pre-migration commit if you must temporarily use the old tree
   layout — prefer fixing forward on `infra/aws/` instead.
