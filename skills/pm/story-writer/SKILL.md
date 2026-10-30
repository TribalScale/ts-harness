---
name: story-writer
description: Turn an approved PRD into a numbered Jira plan (jira_from_prd plan_stories) - epic, user-facing, technical and enabler stories with inline acceptance criteria and Blocks order - for human approval before anything is filed.
category: pm
allowed_agents: [pm-agent]
version: 1.0.0
---

# Story writer

Use on `jira_from_prd` `plan_stories`, including its revisions. This stage only reads Jira
(`jira_search`, `jira_get_issue`, `jira_get_all_projects`); nothing is created until a human
approves the plan and `file_jira` runs. Write every issue body in the house format from
`jira-ticket-format` (read `references/story.md` and `references/epic.md`).

## Inputs

- `input.approved_prd`: the approved PRD. Its `FR-n`, `DoD-n` and Technical Constraints are the source of truth.
- `input.prd_review`: the review. Carry each `critical_gaps` item into `open_questions` unless the PRD already answers it.
- `input.jira_epic_key`: an existing epic to add to, if set.
- `input.jira_project_key`: the project the runtime resolved from the request and tenant config. `input.jira_allowed_projects`: every project the tenant may file in.
- `input.default_issue_type`, `input.issue_type_by_project`: issue types from tenant config.
- On revision: `input.prior_artifact` is the last plan and `input.comments` the reviewer's feedback. Change what was asked; keep numbering stable unless the change requires a renumber, and say so.

## 1. Find the epic and the project

- With `jira_epic_key`: `jira_get_issue` it. It must be an open Epic; inherit its project and read its child issues (`jira_search` with `parent = <key>`). Reuse existing stories; never propose a duplicate.
- Without one, with `input.jira_project_key`: use it.
- Otherwise: choose from `input.jira_allowed_projects` when set, else from `jira_get_all_projects` with `action: create`. One project: use it. Several: pick the best match and list the others in `open_questions`. Never plan into a project outside `input.jira_allowed_projects`.
- Without an epic key, search the chosen project for an open epic or stories on the same feature (`summary ~ "<feature>"`, `statusCategory != Done`). A match is reported in `open_questions`, not silently reused or duplicated.

## 2. Propose the story list

Build it from the PRD: Proposed Solution, Key User Flows, Personas & Permissions, every FR, the
Definition of Done and Technical Constraints.

- Number stories `1.1` to `1.n` with no gaps. Each has a short title, a shape (`user-facing`, `technical` or `enabler`), the FRs it implements, and what it waits on.
- Every FR lands in at least one story. Nothing outside Non-Goals is dropped; nothing in Non-Goals is planned.
- A cross-cutting rule (audit, permissions, a limit) becomes either its own story, when something has to be built, or acceptance criteria inside each affected story. State which.
- Backend work that only unblocks one user-facing story is an **enabler** story that blocks it.
- **Size check:** up to 9 stories is normal. 10 to 12 is allowed but goes in `warnings` with where you would split. More than 12 is not allowed in one epic: plan the first coherent half only (12 or fewer), and state at the top of `plan_markdown` and in `warnings` that the epic must be split and what the second epic covers. More than 10 tickets in total also needs the human's explicit approval at the gate, so say so in `plan_markdown`.

## 3. Pin what a builder needs, per story

A coding agent sees only its ticket. Before writing acceptance criteria, pin:

- **Fields affected**: the exact data the story reads or writes, user-meaningful fields first.
- **Interaction**: the screen or flow to build against and the explicit changes from it.
- **Inputs and dependencies**: the capability it consumes, named as a capability, never as another story.

Collect every unresolved product question into one `open_questions` list, each with the answer you
would pick. A question the PRD lists as open and that blocks a story is never settled silently.

## 4. Write each story

Follow `references/story.md` for the shape. Acceptance criteria are Gherkin, inline, owned by the PM
at the gate:

- every scenario has a Given, a When and a Then; Thens are observable and quote exact strings;
- one scenario per state; at least one error, validation, permission or empty-state scenario where one genuinely applies;
- deterministic behaviour only; probabilistic behaviour goes in `## Eval criteria` as targets.

House style: never name another story in a body (waits are `blocked_by`, filed as `Blocks` links);
cite epic invariants by id instead of restating them; one fact, one section; state decisions, not
the argument for them; no drafting narration.

## 5. Self-check

- Every FR is covered, and the coverage table in `plan_markdown` shows it.
- Every story has a negative scenario or a reason it has none, and no open "Assumption to validate".
- `blocked_by` has no cycles and names only stories in this plan or existing issue keys.
- Size is within limits; issue types exist for the project.

## Output

```json
{
  "plan_markdown": "## Jira plan: <feature>\nProject: <KEY> · Epic: new\n\n| # | Story | Shape | FRs | Blocked by |\n...\n\n### FR coverage\n| FR | Stories |\n...\n\n### Open questions\n...",
  "project_key": "<KEY>",
  "epic": {"existing_key": "", "summary": "<feature>", "description": "<epic body per references/epic.md>"},
  "stories": [
    {
      "number": "1.1",
      "summary": "<title, 72 characters or fewer>",
      "shape": "user-facing",
      "issue_type": "Story",
      "fr_ids": ["FR-1", "FR-3"],
      "blocked_by": [],
      "labels": ["prd", "<component-area>"],
      "description": "<full story body in Markdown>"
    }
  ],
  "open_questions": ["<question> (suggested answer: <...>)"],
  "warnings": []
}
```

`plan_markdown` is what the human reads at the gate: the story table, the FR coverage table, open
questions and the review's critical gaps. Keep full bodies in `stories[].description`.
