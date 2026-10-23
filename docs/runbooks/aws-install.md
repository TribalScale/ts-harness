# AWS install

Provision TS Harness on AWS (EC2 + EBS + Secrets Manager + optional RDS)
via the unified installer.

## Prerequisites

- Terraform `>= 1.5`, AWS CLI credentials for the target account/region
- Remote state bucket (S3) matching `terraform_state` in the install YAML
- Install config with `cloud_provider: aws` and `aws_account_id`
  (start from `config/install/aws.example.yaml`)

## Install

```bash
export INSTALL_CONFIG=/path/to/aws-install.yaml

./scripts/install.sh validate --cloud aws --config "$INSTALL_CONFIG"
./scripts/install.sh plan     --cloud aws --config "$INSTALL_CONFIG"
./scripts/install.sh apply    --cloud aws --config "$INSTALL_CONFIG"
```

Terraform root: `infra/aws/` (do not apply from the legacy `terraform/` pointer).

After apply:

1. Populate Secrets Manager placeholders under `${secret_prefix}/<logical-key>`.
2. SSH using the `operator_connection_command` / `ssh_command` output (port **2222**).
3. Confirm data disk mount: `/vol/ts-harness-data`.
4. Copy your `config/organization.yaml` to the host and re-render the workspace;
   bootstrap otherwise uses the example organization. See
   [Organization config](install.md#organization-config).
5. Run `scripts/ops/post-deploy-smoke.sh` (see [`post-deploy-smoke-test.md`](post-deploy-smoke-test.md)).

Upgrading an install created before the TS Harness rename: follow the checks in
[`install.md`](install.md#upgrading-an-existing-install-to-the-ts-harness-names).

## Database availability

RDS is Multi-AZ by default.

- On an existing install, the next apply converts the database. RDS waits for the maintenance window unless `managed_db.apply_immediately` is true. The conversion ends with a failover that lasts about a minute or two.
- Cost roughly doubles at the database tier.
- Dev accounts set `managed_db.multi_az: false`.
- The app VM stays a single instance. Host failure is covered by the EC2 auto-recover alarm in `infra/aws/monitoring.tf`.

## Upgrade / root migration rehearsal

Existing installs that still reference the old `terraform/` working directory
should rehearse with:

```bash
export AWS_UPGRADE_CONFIRM=1
export EXPECTED_ACCOUNT_ID=123456789012
export EXPECTED_REGION=us-east-1
./scripts/ops/aws-install-upgrade-smoke.sh
```

Use `--dry-run` first. Details: [`aws-terraform-root-migration.md`](aws-terraform-root-migration.md).

## Destroy

```bash
./scripts/install.sh destroy --cloud aws --config "$INSTALL_CONFIG" --confirm <environment>
```

`--confirm` must equal the `environment` field in the install config.

## Runtime config (Slack, OIDC, timezone)

App knobs are not baked into user-data. After changing those tfvars, apply
Terraform (SSM update, no instance replace) and run
`scripts/ops/sync-runtime-config.sh` on the host. See
[`runtime-config.md`](runtime-config.md).

## Related

- Common workflow: [`install.md`](install.md)
- Runtime config: [`runtime-config.md`](runtime-config.md)
- Troubleshooting: [`cloud-troubleshooting.md`](cloud-troubleshooting.md)
- Compatibility matrix: [`../compatibility.md`](../compatibility.md)
