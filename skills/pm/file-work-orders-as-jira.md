---
name: file-work-orders-as-jira
description: File one unassigned Jira issue per work order with a pointer block and dependency links.
---

# File work orders as Jira

Use on `file_work_orders_as_jira`.

## choose_epic

- If `jira_epic_key` is set, fetch it. It must be an open Epic. Inherit its project. Do not create a second epic.
- If it is not an epic or is closed, stop and ask.
- If no epic key, list Jira projects the **mapped PM** can create issues in (`jira_get_all_projects` filtered by create permission). Confirm: “File a new epic plus N issues in PROJECT?”

## file_tickets

1. Human must approve the pack and the file action before any Jira write.
2. Create an Epic when needed. Create one issue per work order.
3. Issue type = `input.issue_type_by_project[project]`, else `input.default_issue_type`, else `Story`. If that type does not exist, stop.
4. **Do not set assignee.**
5. Description = the single work-order section (Summary, Requirements, Files, E2E Acceptance Tests, Out of Scope) plus the pointer block. Do not replace that section with a one-line acceptance criterion.

```text
WO: WO-003
Repo: org/checkout-service@dev
Program: <root_program_id>
Library: https://drive.google.com/...
Manifest: https://docs.google.com/...
```

   `Program:` is the task input `root_program_id` (the program that owns the brief), not this filing program's id. `jira_to_pr` uses it to attach each ticket's program as a child.

6. Link WO dependencies with `jira_create_issue_link` type `Blocks`.
7. Return `jira_epic_key` and `jira_story_keys`. List the keys in the channel as unassigned.
