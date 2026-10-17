# GitHub Branch Protection for Governed Delivery

TS Harness never merges. A human approval is still required, and no agent
may approve or merge. GitHub enforces the merge rules. Configure each target
repo's base branch as below before running governed delivery against it.

## Required status checks

Protect the base branch (for example `dev` or `main`) with a ruleset or classic
branch protection:

| Setting | Value |
|---|---|
| Require a pull request before merging | On |
| Required approvals | At least 1 |
| Dismiss stale approvals when new commits are pushed | On |
| Require status checks to pass | On |
| Required checks | The repo's CI checks, plus `harness/code-review` and `harness/security-scan` |
| Require branches to be up to date | Recommended |
| Allow bypass | Nobody, or break-glass admins only |

List the same CI check names in Settings under
`merge_readiness.required_checks` for the repo (see
[`system-settings.md`](system-settings.md#delivery-keys)). TS Harness's `await_pr_checks`
wait then arms the merge wait only after those checks pass on the PR head SHA.
Without an entry, every check reported on the head SHA must pass.

## `harness/independent-review`

TS Harness posts this commit status on the PR head SHA:

| State | When |
|---|---|
| `success` | An approving reviewer is someone other than the program's PRD approver |
| `success` (`disabled by tenant setting`) | The program was created with `governance.independent_review.enabled: false` |
| `pending` | No approval yet, the only approver is the PRD approver, or an approver is not mapped in `identity.github_logins` |

The PRD approver comes from the recorded `approve_prd` gate decision on the root
program. Reviewers are matched to people through `identity.github_logins`
(email → GitHub login), so map every reviewer's login in Settings → Identity.

The switch is on by default. Only a tenant admin can turn it off, the Activate
call needs a `governance_reason`, and the change is audited as
`governance_setting_changed`. Each program pins the value when it starts, so a
change affects only programs created afterwards. TS Harness still posts the status.
It stays optional in branch protection; when disabled TS Harness posts `success`.

## Webhook and token

Point a repo or org webhook at `POST /webhooks/github` with the secret in
`GITHUB_WEBHOOK_SECRET`, and subscribe to these events:

- `pull_request` (merge detection)
- `pull_request_review` (recomputes `harness/independent-review`)
- `check_suite`, `check_run`, `status` (checks waits on PR and merge SHAs)
- `deployment_status` (release deploy waits)

Without a public webhook, the `poll-github-merges` system job polls merges,
checks, deployments, and wave joins every minute.

With `GITHUB_WEBHOOK_SECRET` set, deliveries without a valid
`X-Hub-Signature-256` get 401 and deliveries without `X-GitHub-Delivery` get
400. A delivery id that was already handled in the last 24 hours returns
`{"status": "duplicate"}` without being processed again. Ids are recorded only
after a delivery succeeds, so **Redeliver** in GitHub still works after a
failure. The id cache is in memory, so a gateway restart forgets it. Without the
secret, the endpoint requires the gateway token instead; set the secret for any
public webhook.

`GITHUB_TOKEN` needs, on each target repo:

| Permission | Used for |
|---|---|
| Contents: read and write | Branch push; deleting TS Harness branches on cancel when enabled |
| Pull requests: read and write | Opening PRs, reading reviews, commenting and closing on cancel |
| Commit statuses: read and write | Posting `harness/independent-review`, `harness/security-scan` and `harness/code-review` |
| Checks: read | Reading check runs. Fine-grained tokens cannot be granted it; on a 403 TS Harness reads GitHub Actions jobs instead (Actions: read), so only Actions-produced checks are visible |
| Actions: read and write | Check results through Actions jobs; `workflow_dispatch` release triggers |
| Deployments: read and write | Deployments API release triggers and status polling |
| Metadata: read | Reading the repo default branch (granted to every fine-grained token) |
| Administration: read | Reading branch protection for required contexts (required when `merge_readiness.policy.require_branch_protection` is true) |

TS Harness only acts on repos inside the tenant binding (`github.org` and
`repo_picker` in [system settings](system-settings.md#delivery-keys)). Releases
to a repo outside it block in `plan_release`. Cancel cleanup never closes PRs or
deletes branches outside it, and does nothing on GitHub when no binding is
configured. Scope the token to the bound repos as well.

## Jira account

Cancel cleanup and Jira filing use `ATLASSIAN_SITE_URL`, `ATLASSIAN_EMAIL`, and
`ATLASSIAN_API_TOKEN`. The account needs these project permissions on each
project in `jira_filing.allowed_projects`:

| Permission | Used for |
|---|---|
| Browse projects | Reading issues and available transitions |
| Create issues | Filing tickets from the PRD |
| Edit issues | Adding the `harness-cancelled` label |
| Add comments | The cancellation comment |
| Transition issues | Closing issues when `compensation.jira: close` |

Issues outside `jira_filing.allowed_projects` are skipped during cleanup.

## Verify

1. Open a test PR from a TS Harness work order. The PR shows `harness/code-review` and
   `harness/security-scan` as required, and `harness/independent-review` as pending.
   The merge button stays blocked until the required checks pass and a human
   approves. No agent may approve or merge.
2. Approve as the PRD approver: `harness/independent-review` stays pending.
3. Approve as a different mapped reviewer: `harness/independent-review` turns
   `success`. The PR merges once the required checks are green and a human
   merges it.
4. `program_status` for the program shows the PR under `external_refs`, and any
   merge-readiness warnings under `warnings`.
