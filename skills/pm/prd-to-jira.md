---
name: prd-to-jira
description: Path from a PRD to Jira (prd_request, then jira_from_prd), and the file_jira stage that files the approved plan exactly - epic, stories, Blocks links and a traceability comment.
category: pm
allowed_agents: [orchestrator, pm-agent]
version: 2.0.0
---

# PRD to Jira

## The path

| Workflow | Stage | Skill | Writes Jira |
|---|---|---|---|
| `prd_request` | `draft_prd`, `revise_prd` | `prd-drafter` | no |
| `prd_request` | `approve_prd` | human gate | no |
| `jira_from_prd` | `review_prd` | `prd-review` | no |
| `jira_from_prd` | `plan_stories` | `story-writer` | no (reads only) |
| `jira_from_prd` | `approve_plan` | human gate; reject revises `plan_stories` | no |
| `jira_from_prd` | `file_jira` | this skill | yes |

Jira is written only in `file_jira`, after a human approved both the PRD and the plan. Outside a
workflow, follow the same order and ask for each approval in the thread; never infer approval from
silence, positive sentiment or "continue".

Do not use this skill to implement a ticket (`skills/dev/jira-to-pr.md`), for sprint or standup
reports, or to update an existing ticket's status.

## file_jira

`input.approved_plan` is the approved `plan_stories` output: `project_key`, `epic`, `stories` and
`plan_markdown`. File it exactly. Do not re-plan, add, drop, merge or reword stories; if the plan
cannot be filed as approved, stop and return `status: blocked` with the reason.

1. **Check before writing.** `project_key` must be in the tenant's allowed Jira projects. If the plan has more than 10 tickets, or any P0/P1 priority or cross-project link, the gate approval must have said so explicitly; otherwise stop.
2. **Do not file twice.** Search first: `jira_search` with `project = <KEY> AND labels = prd AND summary ~ "<epic summary>"`, and the epic's children if it exists. A story already filed from this plan (same summary under the same epic) is reused, not recreated. This stage does not retry automatically, so a partial run is resumed by this check.
3. **Epic.** With `epic.existing_key`, use it. Otherwise `jira_create_issue` with `issue_type: Epic`, `summary`, `description` = `epic.description`, labels `prd` and the component area.
4. **Stories.** One `jira_batch_create_issues` call (up to 50) with, for each story: `project_key`, `issue_type`, `summary`, `description` exactly as approved, `labels`, and `parent_key` = the epic. Never set an assignee. Map each plan `number` to its new key.
5. **Order.** For each `blocked_by` entry, `jira_create_issue_link` with `link_type: Blocks`, `outward_issue_key` = the issue that must land first, `inward_issue_key` = the story that waits. An external dependency gets `link_type: Relates` and the label `external-dependency`.
6. **Coverage.** Fill the epic's `Requirements covered` table with the new keys using `jira_update_issue` on the epic description.
7. **Traceability comment** on the epic with `jira_add_comment`:

   ```markdown
   PRD decomposition (filed by pm-agent after plan approval)

   | Key | Story | Shape | FRs |
   |---|---|---|---|
   | TFS-843 | 1.1 <title> | user-facing | FR-1, FR-3 |

   Open questions carried from the plan: <list or "none">
   ```

8. **Remember.** `remember_memory` with tags `prd`, the component area and the epic key: feature, epic, story count, key scope decisions.

Respect the 50 Jira writes per hour cap from `RULES.md`; a plan that would exceed it is split across runs.

## Output

```json
{
  "jira_epic_key": "TFS-842",
  "jira_story_keys": ["TFS-843", "TFS-844"],
  "summary": "Filed TFS-842 with 2 stories and 1 Blocks link.",
  "epic_url": "https://<site>.atlassian.net/browse/TFS-842",
  "warnings": []
}
```

List the keys in the channel as unassigned; engineers pick stories up with `jira_to_pr`.
