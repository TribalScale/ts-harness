# GitHub App

TS Harness clones, pushes and opens pull requests with a GitHub App. Each coding run gets an installation token for its one repository, valid for an hour and refreshed while the run lasts. Without an App, the `github-token` PAT is used for everything, as before.

## Register the App

Create the App under the organization (**Settings → Developer settings → GitHub Apps → New GitHub App**):

| Field | Value |
|---|---|
| Homepage URL | The admin console URL |
| Webhook | Tick **Active**. URL: `https://<public gateway host>/webhooks/github` (see [Webhook](#webhook) for hosts without a public address) |
| Webhook secret | Leave blank; `scripts/github-app-webhook.py` sets it from Secrets Manager (see [Webhook](#webhook)) |
| Repository permissions | Contents, Pull requests, Commit statuses, Actions, Deployments and Administration: read and write. Checks and Metadata: read. Administration write lets `scripts/repo-governance.py --apply` set branch protection; with read only, an admin has to apply it. See [GitHub branch protection](github-branch-protection.md#webhook-and-token) |
| Subscribe to events | Pull request, Pull request review, Issue comment, Check suite, Check run, Status, Deployment status. Installation events are always sent |
| Where can this App be installed | Only on this account, unless several organizations use this install |

Generate a private key on the App page after creating it.

## Configure the install

1. Store the secrets (AWS shown; on GCP use `<prefix>-github-app-…` secrets):

   ```bash
   aws secretsmanager put-secret-value --secret-id "$PREFIX/github-app-private-key" --secret-string file://app.private-key.pem
   aws secretsmanager put-secret-value --secret-id "$PREFIX/github-app-webhook-secret" --secret-string "$WEBHOOK_SECRET"
   ```

2. Add the App to the install YAML and re-run the installer:

   ```yaml
   github_app:
     app_id: "123456"
     slug: ts-harness-acme
     # host: github.acme.dev   # GitHub Enterprise Server only
   ```

3. Re-run bootstrap phase 10 and restart the runtime (then memory-api and workflow-runner) so the runtime env picks up `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` and `GITHUB_APP_WEBHOOK_SECRET`.

4. In the admin console, open **System → GitHub** and click **Install on an organization**. Pick the repositories coding runs may change. After installing, **Sync installations** loads them if the webhook is not reachable yet.

Keep `github-token` populated during the move. Repositories the App is not installed on keep using it; remove it once the GitHub page lists every repository you deliver to.


## Changing the App's permissions

GitHub does not apply a permission increase (for example Administration read → read and write)
to existing installations until an organization owner accepts it: **Organization settings →
GitHub Apps → (the App) → Configure**, then accept the "review and accept new permissions"
banner. Until then, calls that need the new permission return 403. Tokens the runtime mints
afterwards carry the new permission; nothing needs restarting.

Check what the installation actually has (inside the agent-runtime container):

```bash
python3 - <<'PY'
from ts_harness.integrations.github_app import app_for
app = app_for()
iid = app.installation_id("<owner>", "<repo>")
print(app._call("GET", f"/app/installations/{iid}", bearer=app.jwt())[1]["permissions"])
PY
```

## Webhook

The webhook tells TS Harness about pull requests, reviews, checks and deployments as they
happen. Without it, merges and checks are still found by the `poll-github-merges` job every
minute, but **PRs people open by hand on a protected branch stay blocked**: only the
`pull_request` event lets TS Harness post `success` ("Not a TS Harness PR") on the required
`harness/*` checks. See [GitHub branch protection](github-branch-protection.md#webhook-and-token).

### One App, one webhook URL

The webhook belongs to the App, not to a repository or a host. Every installation of the App
(every organization and repository it is installed on) sends its events to that one URL. If two
TS Harness hosts share one App, only the host the URL points at receives events, and the other
falls back to polling and cannot clear the checks on hand-opened PRs. Give each host its own
App, or keep one host per App.

### 1. Store the secret

```bash
openssl rand -hex 32 | tr -d '\n' > /tmp/wh && \
aws secretsmanager put-secret-value --secret-id "$PREFIX/github-app-webhook-secret" --secret-string file:///tmp/wh && rm /tmp/wh
```

Load it into the runtime: on the host, delete `$DATA_DIR/.setup/phase-10-fetch-secrets.done`
and `phase-12-compose-up.done`, then re-run phases 10 and 12 (see
[Re-run a bootstrap phase](re-run-bootstrap-phase.md)). Check it arrived without printing it:

```bash
sudo docker exec docker-agent-runtime-1 sh -c 'echo ${#GITHUB_APP_WEBHOOK_SECRET}'   # 64
```

### 2. Make `/webhooks/github` reachable

GitHub must reach `POST /webhooks/github` over HTTPS. A host with a public load balancer uses
that. A host that is only on the Tailscale network can expose that one path with
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel):

```yaml
# install YAML
github_app:
  app_id: "123456"
  slug: ts-harness-acme
  webhook_funnel: true
```

Bootstrap phase `12b-github-webhook` then runs, on every build of the host:

```bash
tailscale funnel --bg --https=8443 --set-path=/webhooks/github http://localhost:8080/webhooks/github
python3 /app/scripts/github-app-webhook.py --url https://<tailscale name>:8443/webhooks/github   # in agent-runtime
```

- Funnel is per port, so the webhook gets its own port (8443). The admin console on 443 stays
  tailnet-only. Never add other paths to 8443, and never turn Funnel on for 443.
- The URL contains the host's Tailscale name, which changes when the host is rebuilt. The phase
  re-points the App at the new URL each time, so nothing changes by hand.
- The tailnet policy must allow Funnel for the node (`nodeAttrs` with `funnel`). If it does not,
  the phase logs a warning and the host keeps polling.
- On an existing host, set `GITHUB_WEBHOOK_FUNNEL=true` in the runtime config (Terraform variable
  `github_webhook_funnel`, then `scripts/ops/sync-runtime-config.sh`), or run the two commands above.

### 3. Turn the webhook on and pick events (GitHub UI, once per App)

GitHub's API can set the URL and secret but cannot turn the webhook on or change events.
In **Developer settings → GitHub Apps → (the App)**:

1. **General → Webhook**: tick **Active**, set the URL, leave the secret blank, save.
2. **Permissions & events → Subscribe to events**: Pull request, Pull request review, Issue
   comment, Check suite, Check run, Status, Deployment status. Save.

Then set the secret so it matches Secrets Manager (phase 12b does this too):

```bash
sudo docker exec docker-agent-runtime-1 python3 /app/scripts/github-app-webhook.py --url https://<host>/webhooks/github
```

The script prints the URL, the subscribed events and the last deliveries, and an `ACTION:` line
for anything still to change by hand. It exits 2 when something needs the App settings.

### 4. Verify

1. In the App's **Advanced** tab, redeliver the latest `ping`; it should get 200 (401 means the
   secrets differ: re-run the script).
2. From outside the tailnet, only the webhook answers:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>:8443/webhooks/github   # 401 (unsigned)
   curl -s -o /dev/null -w '%{http_code}\n' https://<host>:8443/                         # 404
   ```

   A fresh Funnel can fail the TLS handshake for a minute or two while its certificate is issued.
3. Open a PR by hand into a protected branch. Within seconds `harness/code-review`, `harness/qa`
   and `harness/security-scan` show `success` "Not a TS Harness PR". Close it.

## How tokens reach coding runs

- On the host, the runtime mints a token for the run's repository before cloning and pushing.
- ECS workers call `POST /internal/coding-jobs/{id}/github-token` on the runtime with their job token. The runtime checks the job token with the memory API, which returns the job's repository, and mints a token for that repository only. Workers refresh ten minutes before expiry. A 404 means no App covers the repository, and the worker keeps the PAT it was started with.
- Commits carry a `Co-authored-by` trailer for the person who asked for the work, from `identity.github_logins` or, with the `auth0_management` directory source, the GitHub login of people who sign in through Auth0's GitHub connection.

## Enterprise Server

`github.com` (or `github_app.host`) comes from the install. Other hosts are stored in `github_app_registrations`, with the private key and webhook secret in the secret manager; the runtime picks the registration from the repository URL's host. Webhook deliveries from Enterprise Server carry `X-GitHub-Enterprise-Host`, which selects the webhook secret.

## Rate limits and polling

The GitHub page shows the last observed rate limit per host; Prometheus exports it as `github_rate_limit_remaining`. Calls that hit a secondary limit or 429 wait as GitHub asks (at most 60 seconds) and retry twice.

While webhooks arrive, the minute-by-minute merge poll drops to a reconciliation pass every `github_reconcile_interval_seconds`. If deliveries stop for an hour, polling resumes every minute.

## Failures

| Symptom | Cause | Fix |
|---|---|---|
| Coding run fails with "GitHub credentials are required" | Neither the App nor `github-token` covers the repository | Install the App on the repository, or populate `github-token` |
| Push fails with 403 for one repository | The installation does not include it | Add the repository to the installation; the change applies within a minute |
| Webhooks return 401 | Secret mismatch | Re-run phases 10 and 12 if the secret changed, then `github-app-webhook.py --url …` to set the App's secret to match |
| `github-app-webhook.py` says the webhook is off (404 on `/app/hook/config`) | **Active** is unticked in the App settings | Tick it, save, re-run the script (or phase 12b) |
| PRs opened by hand stay blocked on `harness/*` checks | No `pull_request` event: webhook off, event not subscribed, or URL unreachable | Run `github-app-webhook.py` and fix its `ACTION:` lines; check the App's recent deliveries |
| `branch protection` apply returns 403 | Installation has Administration read only | Grant read and write on the App, then have an org owner accept it ([above](#changing-the-apps-permissions)) |
| Funnel URL fails TLS (`SSL_ERROR_SYSCALL`) | Certificate still being issued, or Funnel not allowed for the node | Wait two minutes; check `tailscale funnel status` and the tailnet policy |
| GitHub page shows "Not configured" with the YAML set | Private key missing or not loaded | Check `github-app-private-key` is populated and phase 10 ran; the runtime log has `GITHUB_APP_ID` without a key |
| Worker log shows `coding_job_github_token_failed` | Runtime unreachable from the worker | On ECS, check the internal load balancer rule for `/internal/coding-jobs/*/github-token` and the runtime target group health |
