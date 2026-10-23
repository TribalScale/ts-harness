# GitHub App

TS Harness clones, pushes and opens pull requests with a GitHub App. Each coding run gets an installation token for its one repository, valid for an hour and refreshed while the run lasts. Without an App, the `github-token` PAT is used for everything, as before.

## Register the App

Create the App under the organization (**Settings → Developer settings → GitHub Apps → New GitHub App**):

| Field | Value |
|---|---|
| Homepage URL | The admin console URL |
| Webhook URL | `https://<public gateway host>/webhooks/github` |
| Webhook secret | A random string; store it as below |
| Repository permissions | Contents, Pull requests, Commit statuses, Actions and Deployments: read and write. Checks, Metadata and Administration: read. See [GitHub branch protection](github-branch-protection.md#webhook-and-token) |
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
| Webhooks return 401 | Secret mismatch | Compare the App's webhook secret with `github-app-webhook-secret`, then re-run phase 10 |
| GitHub page shows "Not configured" with the YAML set | Private key missing or not loaded | Check `github-app-private-key` is populated and phase 10 ran; the runtime log has `GITHUB_APP_ID` without a key |
| Worker log shows `coding_job_github_token_failed` | Runtime unreachable from the worker | On ECS, check the internal load balancer rule for `/internal/coding-jobs/*/github-token` and the runtime target group health |
