# Install (common workflow)

TS Harness selects **exactly one cloud** at install time: `aws` or `gcp`.
Both providers share the same bootstrap phases, Docker Compose stack, data
mountpoint (`/vol/ts-harness-data`), ports, and health probes. Provider
differences live under `infra/<cloud>/` and `scripts/cloud/<cloud>/`.

## Quick start

```bash
# Copy and edit an example install contract
cp config/install/aws.example.yaml /path/to/install.yaml   # or gcp.example.yaml

# Validate config + local prerequisites (no cloud apply)
./scripts/install.sh validate --cloud aws --config /path/to/install.yaml

# Plan to a file, review, then apply exactly that plan
./scripts/install.sh plan   --cloud aws --config /path/to/install.yaml --plan-file prod.tfplan
./scripts/install.sh apply  --cloud aws --config /path/to/install.yaml --plan-file prod.tfplan
./scripts/install.sh status --config /path/to/install.yaml
# destroy requires --confirm <environment>
```

The Terraform backend location comes from `terraform_state` in the install
config; the committed `backend.tf` in each root is intentionally empty. For
pipeline runs (AWS), see [Run from CI](aws-install.md#run-from-ci).

For GCP, use `--cloud gcp` and `config/install/gcp.example.yaml` (or your copy).

Dry-run Terraform commands without executing:

```bash
./scripts/install.sh plan --cloud gcp --config /path/to/install.yaml --dry-run
```

## Install contract

- Schema: `config/install-contract.schema.json`
- Examples: `config/install/aws.example.yaml`, `config/install/gcp.example.yaml`
- Validator / normalizer: `scripts/install_contract.py`

The contract requires a single `cloud_provider`, managed DB settings, and an
unambiguous Terraform state location (`s3` for AWS, `gcs` for GCP). Mixed
provider fields are rejected.

## Shared runtime surface (both clouds)

| Item | Value |
|------|--------|
| Data mountpoint | `/vol/ts-harness-data` |
| Gateway port | `18789` (`GET /healthz`) |
| Memory API port | `18792` (`GET /health`) |
| Compose (managed DB) | `memory-api` + `managed-db` + `agent-runtime` |
| Secret catalog | Same logical keys (provider secret IDs differ) |

## After apply

1. Populate required secret placeholders (Anthropic API key, gateway token, …).
2. Wait for bootstrap phases on the VM (`$DATA_DIR/.setup/phase-*.done`).
3. Install your organization config (see [Organization config](#organization-config)).
4. Run [`post-deploy-smoke-test.md`](post-deploy-smoke-test.md) /
   `scripts/ops/post-deploy-smoke.sh`.

## Organization config

Bootstrap clones this repository onto the VM (`~<admin-user>/enterprise-assets`).
`config/organization.yaml` is gitignored, so the clone never has it. Without it,
phase 10b renders the agent workspace from `config/organization.example.yaml`
and logs `WARNING: ... organization.yaml not found`; agents then describe the
example company.

To use your own:

1. Copy the file to the host:

   ```bash
   # AWS
   scp -P 2222 config/organization.yaml <admin-user>@<host>:/tmp/organization.yaml
   # GCP
   gcloud compute scp config/organization.yaml <instance>:/tmp/organization.yaml --zone <zone>
   ```

2. On the host, put it in the assets checkout. It is gitignored there too, so
   `git pull` leaves it alone:

   ```bash
   sudo install -o <admin-user> -g <admin-user> -m 0640 /tmp/organization.yaml \
     /home/<admin-user>/enterprise-assets/config/organization.yaml
   rm /tmp/organization.yaml
   ```

   To keep it outside the checkout instead, export `ORGANIZATION_CONFIG=/path/to/organization.yaml`
   when running phase 10b.

3. Re-run phases `10b-render-workspace.sh` and then `11a-agents-init.sh` with the
   wrapper in [`re-run-bootstrap-phase.md`](re-run-bootstrap-phase.md). Both
   phases notice the changed input and re-render; you do not need to delete
   their markers.
4. Restart the runtime so new sessions load the workspace:
   `sudo docker restart "$(sudo docker ps -qf name=agent-runtime)"`.

Credentials never go in this file; keep them in the secrets manager.

## Names a new install uses

| Item | Value |
|------|--------|
| Data mount | `/vol/ts-harness-data` |
| Config directory | `/vol/ts-harness-data/.ts-harness` |
| Setup log | `/var/log/ts-harness-setup.log` |
| Local database | user `ts_harness`, database `ts_harness_memory`, compose service `ts-harness-db` |
| Managed database | name and user `ts_harness`, unless the install config overrides them |
| Metrics namespace | `TSHarness` |
| GitHub commit statuses | `harness/code-review`, `harness/security-scan`, `harness/independent-review` |

`project_name` and `secret_prefix` default to `ts-harness`. Set them in the
install config to use different cloud resource names.

The runtime reads `ts-harness-runtime.yaml` from the config directory.
Environment variables use the `TS_HARNESS_*` names
([`../compatibility.md`](../compatibility.md)).

## Provider runbooks

- AWS: [`aws-install.md`](aws-install.md)
- GCP: [`gcp-install.md`](gcp-install.md)
- Troubleshooting: [`cloud-troubleshooting.md`](cloud-troubleshooting.md)
- AWS root migration: [`aws-terraform-root-migration.md`](aws-terraform-root-migration.md)
