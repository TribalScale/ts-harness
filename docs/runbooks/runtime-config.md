# Runtime config store

Slack channels, OIDC, timezone, embeddings names, workflow-runner knobs, and
implement-repo settings live in a cloud JSON payload — not in instance
user-data and not in the credentials Secrets Manager catalog.

| Cloud | Store | Name |
| --- | --- | --- |
| AWS | SSM Parameter Store (`String`) | `/${project_name}/runtime-config` |
| GCP | Dedicated Secret Manager secret (Terraform **does** update it) | `${secret_prefix}-runtime-config` |

Terraform writes that JSON from tfvars / install YAML. Bootstrap phase `10a`
fetches it through `cloud_fetch_runtime_config`, writes `instance-env.sh`, and
phase 11 renders `ts-harness-runtime.yaml`.

Do **not** put tokens, API keys, or DB passwords in this payload.

## Change a knob without replacing the VM

1. Edit `infra/aws/terraform.tfvars` (or the install YAML / GCP tfvars): for
   example `slack_dev_channel` or `identity_provider` / `oidc_*`.
2. Apply Terraform from the cloud root:

   ```bash
   cd infra/aws   # or infra/gcp
   terraform plan
   terraform apply
   ```

   Expect an update to the SSM parameter or the GCP secret version. The
   instance must **not** be replaced (`aws_instance.harness` /
   `google_compute_instance.harness` ignore launch-template / startup-script
   drift).    The Terraform principal needs `ssm:PutParameter` /
   `ssm:GetParameter` / `ssm:DeleteParameter` / `ssm:AddTagsToResource` /
   `ssm:ListTagsForResource` on `/${project_name}/runtime-config`, plus
   `ssm:DescribeParameters` (usually on `*`). Secrets Manager rights alone
   are not enough on AWS. Hierarchical names must start with `/`.
3. On the host, pull assets if needed, then hydrate and restart
   **agent-runtime** only:

   ```bash
   cd /home/artemis/enterprise-assets
   sudo -u artemis git pull
   sudo bash scripts/ops/sync-runtime-config.sh
   ```

4. Confirm identity still matches tfvars. If OIDC was applied:

   ```bash
   curl -sS https://<tailscale-fqdn>/admin/auth/config
   ```

   The payload should include `"mode": "oidc"` (or the provider you set). Slack
   channel IDs in the rendered YAML should match the new tfvars.

## New install

First boot runs phase `10a` after `10-fetch-secrets.sh`. The store must exist
before bootstrap (Terraform creates it on apply). Missing or empty JSON fails
closed.

## Related

- AWS install: [`aws-install.md`](aws-install.md)
- GCP install: [`gcp-install.md`](gcp-install.md)
