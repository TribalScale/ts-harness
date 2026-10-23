# Cloud install troubleshooting

Cloud-neutral first; provider specifics below. Common workflow:
[`install.md`](install.md).

## Installer / contract

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `Unsupported --cloud` / unknown provider | Typo or third cloud | Use `--cloud aws` or `--cloud gcp` only |
| Mixed-provider fields | `aws_account_id` set with `cloud_provider: gcp` (or reverse) | Keep a single provider identity per YAML |
| Ambiguous state location | Missing `bucket`/`key`, or wrong `backend_type` | AWS → `s3` + region; GCP → `gcs` |
| Preflight fails on tools | Missing terraform/aws/gcloud | Install CLIs; dry-run does not need live creds |

## Bootstrap / disk

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Wrong-provider metadata error | Reusing `$DATA_DIR` from another cloud | Do not cross-mount provider data dirs; wipe only disposable disks |
| Format refused | Device hint / disk id mismatch | Confirm `data_disk_resource_id` and device hint from Terraform outputs |
| Mount missing | Attachment or startup failed | Check provider console + startup log; mount must be `/vol/ts-harness-data` |

## Secrets

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| Required secret abort | Placeholder never replaced | Populate Anthropic + gateway token (and local DB password if applicable) |
| Managed DB credential fetch fails | Bad `MANAGED_DB_CREDENTIAL_REF` / IAM | Compare Terraform output credential ref; check instance SA/role |
| Optional connector warnings | Slack/Jira/GW unset | Expected; install continues with reduced capability |

Logical secret **keys** are identical on AWS and GCP. Physical IDs differ:
AWS uses `${prefix}/${key}`; GCP uses `${prefix}-${key}`.

## Runtime health

| Check | Expect |
|-------|--------|
| `GET :18789/healthz` | JSON `status: ok` |
| `GET :18792/health` | Memory API healthy |
| Compose overlays | `managed-db` when managed Postgres is configured |

Use `scripts/ops/post-deploy-smoke.sh` and
[`post-deploy-smoke-test.md`](post-deploy-smoke-test.md).

## AWS-specific

- Root must be `infra/aws/` (see [`aws-terraform-root-migration.md`](aws-terraform-root-migration.md)).
- `TERRAFORM_TOO_OLD` from `validate`: S3-native locking needs Terraform
  `>= 1.10`. Upgrade, or set `terraform_state.lock_table` to a DynamoDB table.
- `SECRET_PREFIX_CHANGED` from `validate`: the account already has secrets
  under `<project_name>/` but the install config sets a different
  `secret_prefix`. Set `secret_prefix` to `project_name`. Renaming means
  re-creating every secret and re-bootstrapping the VM, which the installer
  does not do.
- `account ... not allowed` from plan: the credentials belong to a different
  account than `aws_account_id` in the install config.
- `Saved plan is stale` from `apply --plan-file`: the state changed after the
  plan was made. Plan again.
- Upgrade rehearsal: `AWS_UPGRADE_CONFIRM=1 ./scripts/ops/aws-install-upgrade-smoke.sh`
- Bedrock embeddings require `CLOUD_PROVIDER=aws`.

## GCP-specific

- Root must be `infra/gcp/`. Backend settings come from `terraform_state` in
  the install config; `init` prompting for a bucket means Terraform ran
  outside `./scripts/install.sh` without `-backend-config` flags.
- Disposable smoke: `GCP_SMOKE_CONFIRM=1 ./scripts/ops/gcp-disposable-smoke.sh`
- Ensure required APIs are enabled (Terraform `apis.tf`).
- Operator SSH via `gcloud compute ssh` (port 2222 after harden).
