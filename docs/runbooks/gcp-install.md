# GCP install

Provision TS Harness on GCP (GCE + Persistent Disk + Secret Manager +
optional Cloud SQL) via the unified installer.

## Prerequisites

- Terraform `>= 1.5`, `gcloud` authenticated to a project with billing
- Python `3.11` with `pyyaml` and `jsonschema`
- Remote state bucket (GCS) matching `terraform_state` in the install YAML.
  `infra/gcp/backend.tf` is an empty `backend "gcs" {}` block; the installer
  passes `bucket` and `prefix` (from `terraform_state.key`) at `terraform init`.
- Install config with `cloud_provider: gcp` and `gcp_project_id`
  (start from `config/install/gcp.example.yaml`)

## Install

```bash
export INSTALL_CONFIG=/path/to/gcp-install.yaml

./scripts/install.sh validate --cloud gcp --config "$INSTALL_CONFIG"
./scripts/install.sh plan     --cloud gcp --config "$INSTALL_CONFIG"
./scripts/install.sh apply    --cloud gcp --config "$INSTALL_CONFIG"
```

Terraform root: `infra/gcp/`.

After apply:

1. Populate Secret Manager placeholders. Logical keys match AWS; GCP stores
   them as `${secret_prefix}-${logical-key}` (no `/` in secret IDs).
2. Connect with the `operator_connection_command` output (`gcloud compute ssh`,
   SSH port **2222** after harden).
3. Confirm data disk mount: `/vol/ts-harness-data`.
4. Copy your `config/organization.yaml` to the host and re-render the workspace;
   bootstrap otherwise uses the example organization. See
   [Organization config](install.md#organization-config).
5. Run `scripts/ops/post-deploy-smoke.sh`.

Upgrading an install created before the TS Harness rename: follow the checks in
[`install.md`](install.md#upgrading-an-existing-install-to-the-ts-harness-names).

## Database availability

Cloud SQL is REGIONAL by default.

- On an existing install, the next apply converts the instance in place and restarts it.
- Cost roughly doubles at the database tier.
- Dev accounts set `managed_db.multi_az: false`, which selects ZONAL.
- The app VM stays a single instance. GCE uses its default live migration and automatic restart.

## Disposable smoke (non-production)

Safe rehearsal without credentials:

```bash
INSTALL_CONFIG=config/install/gcp.example.yaml \
  ./scripts/ops/gcp-disposable-smoke.sh --dry-run
```

Live disposable project (destructive; requires explicit confirm):

```bash
export GCP_SMOKE_CONFIRM=1
export GCP_PROJECT_ID=my-disposable-project
export INSTALL_CONFIG=/path/to/gcp-disposable.yaml
# Optional: HEALTH_BASE_URL=http://<host>:18789 for automated /healthz polling
./scripts/ops/gcp-disposable-smoke.sh
```

The script runs validate → plan → apply → secret placeholders note → wait →
health checks → destroy. Set `SMOKE_SKIP_DESTROY=1` to leave resources up.

## Destroy

```bash
./scripts/install.sh destroy --cloud gcp --config "$INSTALL_CONFIG" --confirm <environment>
```

## Runtime config (Slack, OIDC, timezone)

App knobs are stored in a dedicated Secret Manager secret
(`${secret_prefix}-runtime-config`), not in the credentials catalog and not
in the GCE startup script. After changing those tfvars, apply Terraform
(secret version update, no VM replace) and run
`scripts/ops/sync-runtime-config.sh` on the host. See
[`runtime-config.md`](runtime-config.md).

## Related

- Common workflow: [`install.md`](install.md)
- Runtime config: [`runtime-config.md`](runtime-config.md)
- Troubleshooting: [`cloud-troubleshooting.md`](cloud-troubleshooting.md)
- Compatibility matrix: [`../compatibility.md`](../compatibility.md)
