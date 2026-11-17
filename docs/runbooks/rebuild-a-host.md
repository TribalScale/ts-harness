# Rebuild a host

A TS Harness host can be replaced (a new instance from the launch template) without losing data
or changing its URLs. This page lists what a rebuild needs, what it keeps, and how to run one.

## What survives a rebuild

| Where | What | Survives an instance replacement |
|---|---|---|
| RDS / Cloud SQL | Programs, workflows, tasks, settings, definitions, audit events, scheduler state (with `TS_HARNESS_STATE_BACKEND=postgres`) | Yes |
| S3 / GCS | Managed skills, agent workspaces, intake objects (with `TS_HARNESS_OBJECT_BACKEND=s3`/`gcs`); Terraform state; the install YAML | Yes |
| Secrets Manager / Secret Manager | Every secret, including `github-app-webhook-secret` and `tailscale-authkey` | Yes |
| Data disk (`/vol/ts-harness-data`) | Rendered config, tools, phase markers, `host-id` | Yes (not deleted on termination) |
| GitHub | App settings (webhook on, events), branch protection | Yes |
| Root disk | Packages, Docker images and volumes, Tailscale state, crontab, `~/enterprise-assets` | **No**: the bootstrap rebuilds it |

Docker volumes on the root disk (`docker_ts-harness-state`, `docker_ts-harness-workflow-definitions`)
only hold copies once state is in Postgres. Check before a rebuild (see [Before you rebuild](#before-you-rebuild)).
Unsaved admin-console drafts live there and are lost.

## What a rebuild needs

### The install YAML

`install.sh apply` stores the install YAML beside the Terraform state
(`s3://<state bucket>/<state key dir>/install-config.yaml`, or `gs://…` on GCP), so any machine
can rebuild the host. The YAML holds secret references, never secret values.

```bash
./scripts/install.sh config diff --config config/install/aws-dev.yaml         # local vs stored
./scripts/install.sh config push --config config/install/aws-dev.yaml         # store by hand
./scripts/install.sh config pull --config config/install/aws-dev.yaml \
    --source s3://<state bucket>/dev/aws/install-config.yaml --region us-east-1  # on a new machine
```

`plan` prints a note when the local file differs from the stored one.

### The branch the host runs

```yaml
runtime:
  enterprise_repo_url: https://github.com/TribalScale/ts-harness.git
  enterprise_repo_ref: dev
```

The bootstrap clones this branch and phase 09 checks it out on every run. `redeploy.sh` prints
the branch it deploys and warns when it differs, which is expected during a live test of a
feature branch. Switch back afterwards (`git checkout -B dev origin/dev`, redeploy).

### A stable Tailscale name

The host joins the tailnet as `vm.tailscale_hostname` (default: `project_name`), so its address
`https://<name>.<tailnet>.ts.net` stays the same after a rebuild. That address is in:

- the admin console URL, and the identity provider's allowed callback, logout and web origin URLs
  ([Auth0 setup](auth0-setup.md));
- the GitHub App webhook URL (phase 12b sets it; [GitHub App → Webhook](github-app.md#webhook));
- the Google OAuth callback, for deployments that use Google sign-in or Workspace OAuth
  (derived from the address unless `google-oauth-callback-url` names a custom domain; cleared when
  no Google OAuth client is configured).

When the old machine is still in the tailnet, the new one would be named `<name>-1`. With an
OAuth client (below), phase 06 removes the offline machine with the same name first. With a
plain auth key, remove it by hand in the Tailscale admin console before the rebuild.

#### Tailscale OAuth client (recommended)

Auth keys expire after at most 90 days, so a rebuild after that fails to join the tailnet. An
OAuth client secret does not expire.

1. In the tailnet policy, own the tag and allow Funnel for it:

   ```json
   "tagOwners": { "tag:ts-harness": ["autogroup:admin"] },
   "nodeAttrs": [{ "target": ["tag:ts-harness"], "attr": ["funnel"] }]
   ```

   Tagged machines belong to the tag, not a person: make sure your ACLs let admins reach
   `tag:ts-harness` (SSH, console).
2. **Settings → OAuth clients → Generate**: scopes **Devices: Core (write)** and **Keys: Auth Keys
   (write)**, tag `tag:ts-harness`.
3. Store the client secret (`tskey-client-…`) as the auth key:

   ```bash
   aws secretsmanager put-secret-value --secret-id "$PREFIX/tailscale-authkey" --secret-string "tskey-client-…"
   ```

4. Install YAML:

   ```yaml
   vm:
     tailscale_hostname: ts-harness-dev
     tailscale_tags: ["tag:ts-harness"]
   ```

Changing the name of a running host changes its URLs: update the identity provider's URLs, then
re-run phases 06, 11, 12 and 12b (or rebuild).

#### Tailscale version

Phase 06 installs a pinned Tailscale version (`TAILSCALE_VERSION`, currently 1.104.6) and holds the
package, so hosts don't pick up a new release unannounced. To move to a newer version, raise the
pin in `scripts/phases/06-tailscale.sh` in a PR, live-test it on the dev host
(`sudo TAILSCALE_VERSION=<new> bash scripts/phases/06-tailscale.sh` after removing its marker),
then merge.

### The data disk's zone

The data disk decides the availability zone and the instance follows it, so a replacement
instance can always attach the disk. Terraform never replaces an existing disk over its zone.
`host_availability_zone` (Terraform variable) picks the zone for a new install only.

## How the bootstrap handles a new instance

Phase markers live on the data disk, but most phases configure the root disk. Right after the
data disk is mounted, the bootstrap compares `/etc/machine-id` with `$DATA_DIR/.setup/host-id`. On
a different instance (or a disk set up before `host-id` existed) it removes the markers of the
host-level phases (02 admin user, 03 firewall, 04 SSH, 05 Docker, 06 Tailscale, 09 checkout,
12 compose, 12a/15 cron, 12b webhook, 14 channels, 16 release gates) so they run again. Phases
whose results live on the data disk, in the database or in the cloud keep their markers.

## Before you rebuild

1. No coding jobs running: `docker ps | grep ts-coding` is empty, and no program is mid-delivery.
2. Host-local state is in Postgres (inside agent-runtime):

   ```bash
   python3 /app/scripts/ops/migrate-host-state.py --verify
   # differences: python3 /app/scripts/ops/migrate-host-state.py --import, then --verify again
   ```

3. Snapshots for a way back:

   ```bash
   aws ec2 create-snapshot --volume-id <data volume> --description "pre-rebuild $(date -u +%F)"
   aws rds create-db-snapshot --db-instance-identifier <db> --db-snapshot-identifier pre-rebuild-$(date -u +%Y%m%d)
   ```

4. The stored install YAML is current (`config diff`), the auth secret is valid (OAuth client, or
   an auth key with time left), and the identity provider lists the stable tailnet URL.

## Rebuild

```bash
./scripts/install.sh plan --cloud aws --config config/install/aws-dev.yaml \
    --replace aws_instance.harness --plan-file rebuild.tfplan
```

Read the plan. Expect `aws_instance.harness` replaced, plus its `aws_volume_attachment`,
`aws_eip_association` and alarms. **`aws_ebs_volume.harness_data` must not be replaced or
destroyed**; stop if it is. Then:

```bash
./scripts/install.sh apply --cloud aws --config config/install/aws-dev.yaml --plan-file rebuild.tfplan --allow-destroy
```

Follow the bootstrap (about 20–30 minutes):

```bash
ssh <host> 'sudo tail -f /var/log/ts-harness-setup.log'     # until "TS Harness bootstrap phases finished"
```

Fix any failure in the repo and re-run the phase; never patch the host by hand.

## After a rebuild

- The admin console signs in at the stable URL.
- `sudo docker exec docker-agent-runtime-1 python3 /app/scripts/github-app-webhook.py` shows the
  current URL; a redelivered ping gets 200; a PR opened by hand gets the harness checks marked passed.
- `crontab -l` (root) lists the metrics and DB credential jobs; Slack connects.
- A small program completes on a test repo ([Live-test a change](#live-test-a-change)).
- `cat /vol/ts-harness-data/.setup-complete` exists.

**Rollback:** if the new instance can't be fixed, create a volume from the data-disk snapshot,
import it in place of the old one (or attach it to an instance launched from the launch
template), and restore RDS from its snapshot only if the database was changed.

## Live-test a change

Deploy a branch to a dev host, run real work orders, then clean up. The tools are in
`scripts/ops/live-test/` (inside agent-runtime at `/app/scripts/ops/live-test/`):

```bash
cd /home/<admin>/enterprise-assets && git fetch && git checkout -B <branch> origin/<branch>
sudo ADMIN_USER=<admin> bash scripts/ops/redeploy.sh       # waits for running ts-coding-* workers first

docker cp WO-501.md docker-agent-runtime-1:/tmp/
sudo docker exec docker-agent-runtime-1 python3 /app/scripts/ops/live-test/start_work_order.py /tmp/WO-501.md \
    --repo https://github.com/<org>/<test repo> --base dev
sudo docker exec docker-agent-runtime-1 python3 /app/scripts/ops/live-test/start_program.py \
    /app/scripts/ops/live-test/examples/two-work-orders.md --base onboard-be
sudo docker exec docker-agent-runtime-1 python3 /app/scripts/ops/live-test/status.py workflow <id>   # last line: STATE …
sudo docker exec docker-agent-runtime-1 python3 /app/scripts/ops/live-test/status.py program <id>
sudo docker exec docker-agent-runtime-1 python3 /app/scripts/ops/live-test/cancel.py --program <id>
```

Afterwards: cancel what you started, close the test PRs and delete their branches
(`gh pr close <n> -d`), and put the host back on its configured branch.
