---
name: jira-to-pr
description: Pick an assigned Jira ticket, hydrate Drive/WO specs, and implement one ticket to a PR against the resolved base branch (default dev).
---

# Jira to PR

Use on `pick_assigned_jira` and `implement_jira_ticket`.

## Identity

Map Slack/OIDC email → `identity.jira_account_ids`. Never match display names. If unmapped, ask for a ticket key. Do not list other people’s tickets.

## Pick

1. If the user already pasted `TFS-184` (or similar), skip the wizard and set `jira_issue_key`.
2. Otherwise query assigned work with configured statuses (default `To Do`, `Open`, `Ready for Dev`, `In Progress`). Not `statusCategory != Done`.
3. List **projects that appear in those issues**, not project membership.
4. After they pick a project, show a flat list grouped by epic:

```text
Project: TFS

Epic TFS-100 — Checkout rewrite
  TFS-184  Add cart retry     To Do        WO-003
  TFS-190  Tax edge cases     In Progress  WO-005  (blocked)

Reply with a ticket key (TFS-184), or paste a key anytime to skip this list.
```

5. Show all assigned tickets, including human-written ones. Hide Blocked from the list. An explicit off-list key still works; warn if status is odd.
6. When `pick_ticket` completes, return `jira_issue_description` (the issue description text) with `jira_issue_key`. If the description has a `Program:` pointer, this program becomes a child of that program. Tickets without a pointer still run and show `owner_program: none`.

## Hydrate and implement

1. `jira_get_issue`.
2. Parse the pointer block; load Drive manifest; else `program_id` → `drive_library`; else implement from the Jira body and say governed docs are missing.
3. Resolve PR **base branch**: engineer override → pointer `@branch` → allowlist `default_branch` → runtime repo match → **`dev`**. Confirm “PR against `dev` on `org/repo`”. Use `main` only if explicit.
4. If open `is blocked by` links remain, warn and require confirm.
5. Materialize FRDs / blueprints / the WO into `docs/` for `implement-work-order`.
6. Implement **only this ticket**. Feature branch `feat/TFS-184-…`. Pass `repo_url` + `base_branch` into coding backend; do not let a generic `main` wipe `dev`.
7. After merge, ask if they want the remaining list. Do not auto-start the next ticket.
