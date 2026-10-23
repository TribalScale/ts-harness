# Jira to PR

Engineer program: list my assigned tickets in a project, implement one, PR against **`dev`** unless a pointer or override says otherwise.

## Start

Signals: `my tickets`, `pick up my jira`, `implement TFS-`.

```text
propose_start_program(program_type=jira_to_pr, entry_phase=pick)
```

With a key: `entry_phase=implement` and `artifacts.jira_issue_key=TFS-184`.

## Identity

Requires `identity.slack_user_emails` plus `identity.jira_account_ids` (email → accountId).
Edit those maps in **System → Settings → Identity** (`#/system/settings/identity`).
See [system-settings.md](system-settings.md). Unmapped users must paste a ticket key.

## Ticket list

`assignee = me AND project = X AND status IN (To Do, Open, Ready for Dev, In Progress)`.

Statuses are configurable on `config.ticket_picker.statuses`, or inherited from
tenant Settings when the program omits the section / sets `mode: inherit`.
Not `statusCategory != Done`. All assigned tickets are shown, grouped by epic.
Blocked is hidden; an explicit key still works with a warning.

## PR base branch

1. Engineer names a branch.
2. Pointer `Repo: org/repo@branch`.
3. Allowlist `default_branch` (omit → `dev`).
4. Runtime `implementation.repos.implement_jira_ticket.base_branch` if the URL matches.
5. `dev`.

Confirm the base in kickoff. After merge, re-show remaining tickets; do not auto-start the next one.
