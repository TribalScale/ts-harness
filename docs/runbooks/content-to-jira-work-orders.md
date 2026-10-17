# Content to Jira work orders

PM/TPM program: paste content → pick repo → compare → Q&A until `ready` → work-order pack on Drive → file unassigned Jira tickets. Engineers implement via [jira-to-pr.md](jira-to-pr.md).

## Start

Slack signals (do not use `create a prd`): `plan work from this content`, `content to work orders`, `file work orders as jira`.

```text
propose_start_program(program_type=content_to_jira_work_orders, entry_phase=intake)
```

Mid-pipeline file: `entry_phase=file_jira` with `work_order_ids` + `work_orders_markdown` (and optional `jira_epic_key`).

## Operator config

Tenant defaults and identity maps are edited in **System → Settings**
(`#/system/settings`). See [system-settings.md](system-settings.md).

Built-in `config.repo_picker.repos: []` **inherits** the tenant allowlist. An
empty tenant list fail-closes. Set `mode: custom` on a managed program only when
that program should use its own list (including an empty fail-closed list).

In the program `config` (or Settings / YAML `program_defaults`):

- `repo_picker.repos` — allowlist `{id, default_branch}`. Default branch is `dev`.
- `jira_filing.default_issue_type` — `Story`
- `jira_filing.issue_type_by_project` — per-project override
- `identity.slack_user_emails` — Slack user → email
- `identity.jira_account_ids` — email → Jira accountId (for creatable-project listing)

## Gates

Human confirms repo, says ready before the plan, approves the WO pack, then confirms filing. TS Harness does not assign tickets.

## Pointers

Each story description includes `WO`, `Repo: org/repo@dev`, `Program`, Drive library/manifest. Dependencies become Jira Blocks links.
