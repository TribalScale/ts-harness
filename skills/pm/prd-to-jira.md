# PRD to Jira — Draft, Approve, Decompose, File

## When to Use

Use this skill when:
- The orchestrator delegates a new product request, feature brief, or "we need a PRD for X"
- A stakeholder provides a problem statement, meeting notes, or research that must become actionable engineering work
- Existing backlog items need a PRD written against them before they can be picked up
- The orchestrator provides an explicitly approved PRD and asks for Jira epic/story creation

Do **not** use this skill for:
- Updating an existing ticket's status or adding comments (use `comms` Jira tooling)
- Implementing a ticket (use `skills/dev/jira-to-pr.md`)
- Sprint or standup reporting (use `skills/comms/sprint-report.md`, `skills/comms/standup-summary.md`)
- Pure documentation tasks with no engineering work

## Operating Modes

This skill has two modes. The orchestrator must choose one before delegation:

1. **Draft / Revise PRD** — write or update a Draft PRD from requirements and human comments. No Jira writes are allowed.
2. **File Approved PRD in Jira** — create the Jira epic, stories, tasks, and traceability comment. This mode requires explicit human approval of the PRD artifact.

If the orchestrator asks for Jira creation but does not include human approval text and the approved PRD artifact, stop and return:

```json
{
  "status": "blocked",
  "warnings": ["Jira creation requires explicit human approval of the PRD."],
  "follow_up_needed": ["Ask the user to review and approve the PRD before filing Jira tickets."]
}
```

---

## Steps

### 1. Recall Memory

Query memory for prior PRDs, product decisions, and team conventions in this area before drafting.

```
memory_recall: project=ts_harness, tags=[prd, product, <component-area>, conventions]
```

Look for:
- Prior PRDs on adjacent features (reuse language, stay consistent)
- Past "non-goals" and explicit scope decisions for this area
- Known constraints (tech, compliance, timeline)
- Preferred story point scale and labeling conventions

### 2. Understand the Request

Before writing anything, confirm you can answer:

- **Who** is this for? (user persona / segment)
- **What problem** are they experiencing today?
- **Evidence** the problem exists (data, support tickets, interviews, analyst report)
- **Desired outcome** — measurable where possible
- **Constraints** — timeline, compliance, tech stack, budget

If any are missing or vague, return to the orchestrator with a clarification request rather than guessing. Ambiguous problem statements produce ambiguous tickets.

For data-backed scoping, request a brief from `analyst-agent` via the orchestrator (e.g., "how many users hit this flow last month?").

### 3. Draft the PRD

Produce the PRD as a single Markdown document using this exact section order:

```markdown
# PRD: <Feature Name>

**Author:** pm-agent
**Status:** Draft
**Last Updated:** <YYYY-MM-DD>
**Linked Epic:** <filled in after step 6>

## 1. Problem Statement
<Who has the problem, what it is, evidence it exists. 3-5 sentences max.>

## 2. Goals
- <Outcome 1 — measurable>
- <Outcome 2 — measurable>

## 3. Non-Goals
- <Explicitly out of scope>

## 4. User Stories
- As a <role>, I want <capability> so that <outcome>.
- As a <role>, I want <capability> so that <outcome>.

## 5. Acceptance Criteria
For each user story, provide Given/When/Then:
- **Story 1:**
  - Given <context>, When <action>, Then <observable outcome>.

## 6. Success Metrics
| Metric                     | Baseline | Target | Measurement window |
| -------------------------- | -------- | ------ | ------------------ |
| <e.g., activation rate>    | <x%>     | <y%>   | 30 days post-launch |

## 7. Open Questions
- [ ] <Question> — owner: <name or role>

## 8. Risks & Dependencies
- **Technical:** <...>
- **Product:** <...>
- **Timeline:** <...>

## 9. Rollout Plan
- Flag name: <feature-flag-name>
- Phasing: <e.g., internal → 10% → 100%>
- Comms owner: comms-agent (release notes, announcement)
```

Every section is mandatory. If a section legitimately has no content, write `_None_` — do not delete it.

### 4. Validate the Draft

Before filing anything in Jira, self-check:

| Check                                                                       | Pass? |
| --------------------------------------------------------------------------- | ----- |
| Every goal is outcome-oriented, not a task list                             |       |
| Every user story is in the `As a / I want / so that` form                   |       |
| Every acceptance criterion is testable and observable                       |       |
| Success metrics have baselines, targets, and measurement windows            |       |
| Non-goals section is not empty (if it is, scope is probably too loose)      |       |
| No PII, credentials, or confidential customer names in the document         |       |
| Confidence on the problem statement is `[HIGH]` or `[MODERATE]`             |       |

If any check fails, revise before step 5.

### 5. Persist Draft for Review

**Governed `prd_request` / delivery program:** return `prd_markdown` in the task output only. Do **not** call `google_workspace_drive_create` — the runtime publishes the PRD into the Google Drive delivery artifact library when the draft is presented in Slack. Do not pass or invent Drive folder IDs.

**Ad-hoc / informal only** (when not in a governed workflow and `google-workspace-docs` tools are available — see `skills/pm/google-workspace.md`):

1. `google_workspace_drive_create` — `name`: `PRD: <Feature Name>`, `file_type`: `document` (omit `folder_id` unless the human provides one).
2. `google_workspace_docs_append` — full PRD markdown, dry run first.
3. After write approval, `google_workspace_docs_append` with `confirmed: true`.

If Google Workspace is unavailable or write approval is denied, continue with markdown-only output and note the skip in `warnings`.

### 6. Return Draft for Human Review

In **Draft / Revise PRD** mode, stop here and return the PRD to the orchestrator.

The response must include:

```json
{
  "status": "ok",
  "result": {
    "prd_title": "<Feature Name>",
    "prd_status": "Draft",
    "prd_markdown": "<full PRD markdown>",
    "google_doc_id": "<document id when saved>",
    "google_doc_url": "<webViewLink when saved>",
    "open_questions": ["<Q1>", "<Q2>"]
  },
  "confidence": "<HIGH|MODERATE|LOW>",
  "warnings": ["<anything the reviewer should know>"],
  "follow_up_needed": ["Human review of PRD"]
}
```

Do **not** create Jira issues, update existing Jira issues, or publish the PRD while in Draft / Revise PRD mode.

### 7. Approval Gate Before Jira

Before any Jira search, issue creation, issue update, or comment write, confirm all of the following are present:

- The orchestrator requested **File Approved PRD in Jira** mode.
- The orchestrator provided the exact PRD artifact to file.
- The orchestrator provided explicit human approval text for that PRD, such as "PRD approved" or "Approved, create Jira tickets."
- Any required extra approvals are present for P0/P1 priority, cross-project links, roadmap commitments, or more than 10 tickets.

If any item is missing, stop and return a blocked response. Never infer approval from silence, positive sentiment, or a request to "continue".

### 8. Check for Duplicates

Search Jira for existing work that overlaps with this PRD. File duplicates as links, not new tickets.

```
jira_search(jql="project = <PROJECT> AND (summary ~ \"<feature name>\" OR description ~ \"<feature name>\") AND statusCategory != Done")
```

If an existing epic covers the same problem:
1. Do **not** create a new epic.
2. Attach the PRD to the existing epic as a comment or linked document.
3. Propose missing stories as additions to that epic.
4. Return to the orchestrator and flag the duplicate before proceeding further.

### 9. Create the Epic

Confirm the target project key `<PROJECT>` is in the tenant's allowed Jira projects, then:

```
jira_create_issue(
  project_key="<PROJECT_KEY>",
  issue_type="Epic",
  summary="<Feature Name>",
  description="<Google Doc URL from draft step when available, otherwise full PRD markdown, or a Confluence link + PRD summary>",
  labels=["prd", "<component-area>"],
  components=["<component>"],
  priority="<Medium unless stakeholder brief specified otherwise>"
)
```

Capture the returned epic key (e.g., `TFS-842`) — it is required for every child ticket and should be reflected as the PRD's linked epic in the returned summary.

Do not reach this step for P0/P1 priority, committed-roadmap work, cross-project links, or more than 10 tickets unless the required extra approvals were already supplied at the approval gate.

### 10. Create Stories (and Tasks if Needed)

Batch-create one story per user story in the PRD. Each story's description must include its acceptance criteria.

```
jira_batch_create_issues(
  issues=[
    {
      "project_key": "<PROJECT_KEY>",
      "issue_type": "Story",
      "summary": "<Story summary, <= 72 chars>",
      "description": "**User Story**\nAs a <role>, I want <capability> so that <outcome>.\n\n**Acceptance Criteria**\n- Given ... When ... Then ...\n\n**Parent PRD:** <Epic key>",
      "labels": ["prd", "<component-area>"],
      "parent_key": "<epic-key>"   // Epic Link
    },
    ...
  ]
)
```

For non-user-facing work surfaced by decomposition (migrations, infra prep, spikes), file as `Task` or `Spike` with the same `parent_key`.

Linking rules:
- Every child ticket must be linked to the epic via `Epic Link` / `parent_key`.
- Cross-dependencies between stories use `jira_create_issue_link` with link types `blocks` / `is blocked by`.
- External dependencies (e.g., waiting on a vendor) get a `relates to` link and a label `external-dependency`.

### 11. Post the Traceability Comment

Add one comment on the epic that lists every child ticket created in this run, so a reviewer can audit PRD coverage in a single click:

```
jira_add_comment(
  issue_key="<epic-key>",
  comment="""
PRD decomposition (filed by pm-agent):

| Story Key | Summary | Maps to PRD section |
|-----------|---------|---------------------|
| TFS-843  | ...     | Story 1             |
| TFS-844  | ...     | Story 2             |
| TFS-845  | ...     | Task: DB migration  |

PRD status: Approved by human. Awaiting sprint planning before engineering commitment.
"""
)
```

### 12. Return to Orchestrator

Provide the orchestrator with a structured summary:

```
{
  "status": "ok",
  "result": {
    "prd_title": "<Feature Name>",
    "epic_key": "<TFS-842>",
    "epic_url": "https://<your-org>.atlassian.net/browse/TFS-842",
    "child_keys": ["TFS-843", "TFS-844", "TFS-845"],
    "ticket_count": 3,
    "labels": ["prd", "<component-area>"],
    "open_questions": ["<Q1>", "<Q2>"]
  },
  "confidence": "<HIGH|MODERATE|LOW>",
  "warnings": ["<anything the reviewer should know>"],
    "follow_up_needed": ["Sprint planning to schedule epic"]
}
```

### 13. Remember

Store the PRD context so future related asks benefit from it.

```
memory_store:
  tags: [prd, <component-area>, <epic-key>]
  content: |
    PRD "<Feature Name>" filed as <epic-key> with N child tickets.
    Problem: <one-liner>.
    Key decisions: <non-goals, scope cuts, open questions>.
    Metrics: <target metric and value>.
    Epic: <url>
```

---

## Rate Limit & Guardrails

- Respect the **50 Jira updates/hour** cap from `RULES.md` — a large PRD may need to be split across runs if it would exceed this.
- Bulk ticket runs that would create **more than 10 tickets** require human approval before step 9.
- **Never** create or update Jira issues before explicit human approval of the PRD.
- **Never** file tickets into projects outside the tenant's allowed Jira projects.
- **Never** set priority to P0 or P1 without human approval.
- **Never** publish a PRD as "Approved" without human approval — `Draft` is the only status pm-agent is allowed to set.

---

## Quick Reference

| Item                 | Convention                                              |
| -------------------- | ------------------------------------------------------- |
| PRD author field     | `pm-agent`                                              |
| PRD initial status   | `Draft` (never `Approved` — requires human)             |
| Jira creation gate   | Explicit human approval of the PRD is required          |
| Epic labels          | `prd`, `<component-area>`                               |
| Story link to epic   | `Epic Link` / `parent_key` (required on every child)    |
| Cross-story deps     | `blocks` / `is blocked by` links                        |
| Max batch size       | 10 tickets per run without human approval               |
| Traceability comment | One per epic, listing every child ticket created        |
