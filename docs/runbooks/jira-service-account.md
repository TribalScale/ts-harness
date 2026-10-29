# Jira service account

TS Harness calls Jira as an Atlassian service account with an OAuth 2.0 client credential. The runtime exchanges the client ID and secret for an access token at `https://auth.atlassian.com/oauth/token`, caches it until a minute before it expires, and calls Jira through `https://api.atlassian.com/ex/jira/<cloudId>`. Without a client credential, the email and API token (`jira-username` and `jira-token`) are used against the site URL, as before.

Two callers use these credentials:

- Cancel cleanup (`JiraClient`): labels, comments and transitions issues.
- The `atlassian` MCP server (`mcporter`), which gives agents these tools:

  | Toolset | Tools | Granted to |
  |---|---|---|
  | `jira-read` | `atlassian_search`, `jira_search`, `jira_get_issue`, `jira_get_all_projects` | orchestrator, pm-agent, dev-agent, comms-agent |
  | `jira-write` | `jira_create_issue`, `jira_batch_create_issues` (up to 50), `jira_create_issue_link`, `jira_add_comment`, `jira_update_issue` | pm-agent |

  Agents see them as `mcp-atlassian-<tool>`. Issues are always filed unassigned.

Jira writes are held to three rules in code:

- Every write must name a project or issue key inside `jira_filing.allowed_projects`, when that list is set.
- Inside a workflow, only stages that declare `allowed_external_effects: ["jira_write"]` can write. The planning call of an expand stage cannot; its approved child tasks can.
- The workflow validator flags stages that can write before a human approval gate. A stage in a workflow that starts from an approved source artifact (for example the approved PRD) counts as gated.

## Create the service account

1. In [Atlassian Administration](https://admin.atlassian.com), open **Directory → Service accounts** and create an account, for example `ts-harness`.
2. Give it access to Jira, and add it to each project in `jira_filing.allowed_projects` with these permissions:

   | Permission | Used for |
   |---|---|
   | Browse projects | Reading issues and available transitions |
   | Create issues | Filing tickets from the PRD |
   | Edit issues | Adding the `harness-cancelled` label; `jira_update_issue` |
   | Add comments | The cancellation comment and traceability comments |
   | Link issues | Dependency links between work-order issues |
   | Transition issues | Closing issues when `compensation.jira: close` |

3. On the service account, create an **OAuth 2.0** credential for Jira with these scopes: `read:jira-work`, `write:jira-work` and `read:jira-user`. `write:jira-work` covers creating and editing issues, comments and transitions. Copy the client secret; it is shown once.

## Store the credential

AWS shown; on GCP use the `<prefix>-jira-…` secrets.

```bash
aws secretsmanager put-secret-value --secret-id "$PREFIX/jira-client-id" --secret-string "$CLIENT_ID"
aws secretsmanager put-secret-value --secret-id "$PREFIX/jira-client-secret" --secret-string "$CLIENT_SECRET"
```

Keep `jira-url` set to the site, for example `https://acme.atlassian.net`. The runtime reads the cloud ID from `<site>/_edge/tenant_info` once at first use. To skip that lookup, store it in `jira-cloud-id`.

The secrets are created by Terraform with a placeholder value. On an existing AWS install, apply only the new secrets and the instance's read policy, which lists each secret by ARN:

```bash
terraform -chdir=infra/aws apply \
  -target='aws_secretsmanager_secret.harness["jira-client-id"]' \
  -target='aws_secretsmanager_secret_version.harness["jira-client-id"]' \
  -target='aws_secretsmanager_secret.harness["jira-client-secret"]' \
  -target='aws_secretsmanager_secret_version.harness["jira-client-secret"]' \
  -target='aws_secretsmanager_secret.harness["jira-cloud-id"]' \
  -target='aws_secretsmanager_secret_version.harness["jira-cloud-id"]' \
  -target='aws_iam_role_policy.secrets_read'
```

Without the policy target the host gets `AccessDeniedException` for the new secrets, and phase 10 warns that it could not read them. IAM can take a couple of minutes to apply the change. On GCP the runtime service account reads every secret in the project, so no policy change is needed.

## Load it on the host

Re-run bootstrap phases 10, 11 and 14 (see [re-run a bootstrap phase](re-run-bootstrap-phase.md)). Phase 10 writes the `ATLASSIAN_*` variables to `secrets.env` and logs which mode it found (`Jira auth: service account (oauth)`). Phase 14 writes them to the runtime `.env` and recreates `agent-runtime`, `memory-api` and `workflow-runner`.

The `atlassian` MCP server receives the same variables through `secret:` references in its `env` block, because MCP servers do not inherit the runtime environment.

| Secret | Runtime variable |
|---|---|
| `jira-url` | `ATLASSIAN_SITE_URL` |
| `jira-cloud-id` | `ATLASSIAN_CLOUD_ID` |
| `jira-client-id` | `ATLASSIAN_CLIENT_ID` |
| `jira-client-secret` | `ATLASSIAN_CLIENT_SECRET` |
| `jira-username` | `ATLASSIAN_EMAIL` |
| `jira-token` | `ATLASSIAN_API_TOKEN` |

## Verify

Inside the runtime container, without printing the token:

```bash
docker exec docker-agent-runtime-1 python3 -c '
from ts_harness.integrations.jira_client import JiraClient
client = JiraClient.from_env()
me = client.request("GET", "/rest/api/3/myself")
print(client.auth.mode, me.get("accountType"), bool(me.get("accountId")))
'
```

Expect `oauth app True`. Then ask an agent to run `jira_search` with a simple query such as `project = TFS ORDER BY created DESC`. To check writes, file a test issue with `jira_create_issue` in an allowed project and delete it in Jira afterwards.

## Fall back to the API token

Set `jira-client-id` back to a single space (or delete its value) and re-run phases 10 and 14. With `jira-url`, `jira-username` and `jira-token` populated, both callers use the email and API token again.

## Rotate the client secret

Create a new secret on the service account's OAuth credential, store it in `jira-client-secret`, re-run phases 10 and 14, then revoke the old one. Cached access tokens expire within an hour; a 401 drops the cached token and retries once with a new one.
