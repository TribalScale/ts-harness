---
name: prd-drafter
description: Draft or revise a PRD on prd_request (draft_prd, revise_prd). Chooses a Lite or Full profile by trigger, numbers requirements FR-n and DoD-n, and keeps acceptance criteria out of the PRD.
category: pm
allowed_agents: [pm-agent]
version: 1.0.0
---

# PRD drafter

Use on `prd_request` `draft_prd` and `revise_prd`, or when asked for a PRD outside a workflow.
Never create or update Jira issues here.

## Inputs

- `input.clarified_requirements.initial_request` is the requester's own words; `answers` refine it and win on conflict.
- `input.intake_output` is what intake asked.
- On `revise_prd`: `input.prior_artifact` is the last draft and `input.comments` is the reviewer's feedback. Change what the comments ask; keep FR and DoD ids stable.

Recall prior PRDs and decisions for the area first (`recall_memory` with tags `prd` and the component area).
If the problem, the persona or the desired outcome is still unknown, return a clarification request instead of guessing.

## Choose the profile

Use **Full** if any trigger holds; otherwise **Lite**. Do not ask the author which they prefer.

- spans more than one product or repository, or depends on another team's deliverable
- adds data entities, states or relationships
- adds or changes permissions or roles
- touches billing, pricing, audit, compliance or personal data
- materially changes load or cost

Name the trigger that decided it in the header. When unsure, choose Lite.

## Template

Headings are not numbered. Never delete a section: write `N/A: <reason>` when it does not apply.
Sections marked (Full) are omitted in a Lite PRD.

```markdown
# PRD: <Feature Name>

**Author:** pm-agent · **Status:** Draft · **Last updated:** <YYYY-MM-DD>
**Profile:** Lite | Full (<deciding trigger, or "no Full trigger">)
**Linked epic:** <filled in after filing>

## Overview
<At most five sentences.>

## Problem Statement
<Who has the problem, what it is, the evidence.>

## Goals
- <measurable outcome>

## Non-Goals
- <explicit exclusion>

## Personas & Permissions
| Persona | Can | Cannot |
|---|---|---|

## Proposed Solution
<Concrete behaviour: screens, states, rules, limits. Not a concept statement.>

## Key User Flows
### <Flow name>
1. <step>
**On failure:** <what the user sees and can do>

## Requirements
- [ ] **FR-1** <requirement with its exact specifics> (Specified in: <section>; Verified by: DoD-2)

## Technical Constraints
- <every framework, language, platform, storage and data choice the requester stated, word for word>

## Non-Functional Requirements (Full)
<performance, accessibility, localization, browser and device support, observability>

## Data / State Model (Full)
<entities, states and transitions>

## Definition of Done
- DoD-1: <verifiable, customer-observable>

## Success Metrics
| Metric | Baseline | Target | Measurement window |
|---|---|---|---|

## Risks & Dependencies (Full)
- <risk or dependency> (owner: <name or role>)

## Rollout Plan (Full)
- Flag: <name or "none"> · Phasing: <...> · Comms owner: comms-agent

## Open Questions
- [ ] <question> (owner: <named person or role, not "team">)
```

## Rules

- **Requirements carry the request forward.** Refinery, work orders and the coding agent copy Requirements and Technical Constraints verbatim. Every named item, number, interval, limit and technology the requester stated appears there exactly.
- **Write Requirements last.** Read every other section back and give each behaviour, state, setting, error case and permission an FR row. A behaviour with no FR is an orphan; an FR with no `Specified in` is unspecified; one with no `Verified by` will ship untested.
- **In a Lite PRD**, `Specified in` points only at Proposed Solution, Key User Flows or Personas & Permissions. If an FR needs a Full-only section, re-check the triggers.
- **Ids are append-only.** Never renumber or reuse `FR-n` or `DoD-n`. Withdraw in place: `FR-3: ~~withdrawn, superseded by FR-9~~`.
- **No Given/When/Then in the PRD.** Acceptance criteria are written per story when the PRD is filed in Jira.
- Definition of Done has 5 to 10 items. Every metric has a baseline and a target, or says why there is none.
- No PII, credentials or confidential customer names. Status is always `Draft`; only a human approves.

## Self-check before returning

- Profile matches the triggers; every required section is present or `N/A: <reason>`.
- Every stated requirement and technology appears in Requirements and Technical Constraints.
- Every FR has `Specified in` and `Verified by`; no orphan behaviour.
- Non-Goals is not empty; every open question has an owner.

## Output

```json
{
  "status": "ok",
  "result": {
    "prd_title": "<Feature Name>",
    "prd_status": "Draft",
    "prd_profile": "lite | full",
    "prd_markdown": "<full PRD>",
    "open_questions": ["<Q1>"]
  },
  "confidence": "HIGH | MODERATE | LOW",
  "warnings": [],
  "follow_up_needed": ["Human review of PRD"]
}
```

Governed `prd_request`: return the PRD in `result.prd_markdown` only; the runtime publishes it
to Google Drive. Outside a workflow, follow `skills/pm/google-workspace.md` to save a Doc, and
continue with Markdown only if that fails.
