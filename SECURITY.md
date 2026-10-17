# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately through
GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**.

Include the affected component (gateway, agent loop, tool policy, sandbox,
memory API, workflow runner, admin console, or install scripts), the version or
commit, and steps to reproduce. Prompt-injection and tool-escape reports are in
scope: for example, an agent calling a tool outside its allowlist, or content
from Slack, Jira, a pull request, or a repository file causing an unapproved
action.

We will acknowledge the report, work on a fix in a private advisory, and credit
you when the fix is published unless you ask us not to.

## Supported versions

Only the latest release on the default branch receives security fixes.

## Handling secrets

- Configuration refers to secrets by name only (`credentials_secret`,
  `client_id_secret`, `client_secret_secret`). Validation rejects pasted keys,
  PEM blocks, and JSON credentials.
- Keep real values in your secrets manager or a local `.env` that is never
  committed.
- `GATEWAY_TOKEN`, `MEMORY_API_TOKEN`, and `SANDBOX_WORKER_TOKEN` are required
  outside local tests. Rotate them as described in
  [`docs/runbooks/secret-rotation.md`](docs/runbooks/secret-rotation.md).
