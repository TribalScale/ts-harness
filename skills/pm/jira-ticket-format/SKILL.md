---
name: jira-ticket-format
description: House shape for Jira epics, stories, enabler stories, tasks and bugs written by pm-agent. Templates are reference files; descriptions are Markdown.
category: pm
allowed_agents: [orchestrator, pm-agent]
version: 1.0.0
---

# Jira ticket format

Every Jira issue pm-agent writes follows one of these templates. Read the one you need with
`read_skill_reference skill_id=jira-ticket-format path=references/<type>.md`:
`epic.md`, `story.md` (user-facing, technical and enabler variants), `task.md`, `bug.md`.

Descriptions are Markdown. The Jira tools convert headings, lists, `- [ ]` checkboxes,
fenced code (use `gherkin` for scenarios), quotes, tables, bold and links.

## Hierarchy

- **Epic**: one per approved PRD. Holds the goal, the requirements it covers, invariants, scope and DoD.
- **Story**: a unit of user or system value under the epic. Acceptance criteria live inline in the story, nowhere else.
- **Enabler story**: backend work that unblocks one named story. Label `enabler`, and a `Blocks` link to the story it unblocks.
- **Task**: technical work with no user-facing behaviour. Subtasks of a user-facing story are checkboxes in the story, not separate issues.
- **Bug**: a defect in shipped behaviour, with evidence and a regression test.

## Rules for every issue

1. **Scope fence.** Say what changes and what does not. Removals are work; list them. State unchanged behaviour when a reader could assume otherwise.
2. **Hard numbers.** Limits, intervals, sizes, counts and exact user-facing strings, quoted. Never "fast" or "a few".
3. **Traceability.** Tag the PRD requirements an issue implements (`FR-3`). Cite epic invariants by id (`AD-2`) instead of restating them.
4. **At least one negative case** (error, validation, permission or empty state) where one genuinely applies. Do not invent a failure path.
5. **Independently executable.** Never name another story in a description. Order and waits are `Blocks` links. The enabler's `Consumed by:` line is the one exception.
6. **No open assumptions.** An unknown becomes a question to the PM before filing. If scope is genuinely unknown, write `Scope: TBD` plus the open questions, never invented acceptance criteria.
7. **Design references are optional.** With no Figma frame, name the state and describe the visual change in one sentence.
8. **State decisions, not arguments.** No drafting narration, no rationale paragraphs, a fact said once in the section that owns it.
9. **Never set an assignee, sprint or story points.** Priority stays Medium unless the PRD or a human says otherwise; P0/P1 needs human approval.
