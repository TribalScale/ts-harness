---
name: prd-drafter
description: Draft or revise a PRD on prd_request (draft_prd, revise_prd). Chooses a Lite or Full profile by trigger, numbers requirements FR-n and DoD-n with a priority, records intake decisions and assumptions, and keeps acceptance criteria out of the PRD.
category: pm
allowed_agents: [pm-agent]
version: 1.6.0
---

# PRD drafter

Use on `prd_request` `draft_prd` and `revise_prd`, or when asked for a PRD outside a workflow.
Never create or update Jira issues here.

## Inputs

- `input.clarified_requirements.initial_request` is the requester's own words; `answers` refine it and win on conflict.
- `input.intake_output` is what intake asked (`prd-intake`). Each question carries a
  `Recommended:` answer. In `answers`:
  - "yes", "yes to all" or similar accepts every recommendation not otherwise answered;
  - an answer to a numbered question replaces that recommendation;
  - a question the reply does not cover, when the reply is not a general yes, is not decided:
    use the recommendation and list it under Assumptions.
- On `revise_prd`: `input.prior_artifact` is the last draft and `input.comments` is the reviewer's feedback. Change what the comments ask; keep FR and DoD ids stable.
- `input.today` is today's date. Use it for **Last updated**; never guess a date.

- `input.sources`, when present, is what the requester attached (files, Drive documents, image descriptions), one markdown section per source. Treat it as part of the request and name a source when the PRD relies on it. A section marked _Could not be read_ or listed under _Not read_ is missing; list it under Open Questions rather than guessing its contents.

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
**Linked epic:** none yet (set when the PRD is filed in Jira)

## Overview
<At most five sentences.>

## Problem Statement
<Who has the problem, what it is, the evidence.>
**Current state:** <how it works today, or "nothing exists">
**Why now:** <the trigger: a deadline, a customer, a dependency, a cost>

## Goals
- <measurable outcome>

## Non-Goals
- <explicit exclusion>

## Decisions
- <decision> (from intake Q<n>: <"accepted recommendation" or the requester's answer>)

## Assumptions
- <what the draft assumes without an answer, and what changes if it is wrong>

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
- [ ] **FR-1** (Must) <requirement with its exact specifics> (Specified in: <section>; Verified by: DoD-2)

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
<or `N/A: <reason>` when nothing is open>
```

## Framing a Full PRD

A Full PRD covers a project several teams will build for months; its reviewers decide from the
first three sections whether the rest is right. In a Full PRD, write them in this form instead
of the short one above. Every row cites its source (a document and section, a rule id, an
intake answer or a decision); never fill a cell with an invented number. When a source has no
number, write what is known and mark it ("not tracked today", "to measure in the first two
weeks").

```markdown
## Problem Statement
<Two to four sentences: the operation, what goes wrong, who feels it.>

| Persona | Problem today | Evidence (source) |
|---|---|---|
| <each persona the project serves> | <what is hard, slow or wrong for them> | <a number or fact, and where it comes from> |

**Current state:** <how the work is done today, step by step: tools, hand-offs, delays>
**Cost of doing nothing:** <hours, late orders, errors, risk, quantified where the sources allow>
**Why now:** <the triggers, with dates>

## Goals
| Goal | Who benefits | Measure | Baseline | Target | By when | Serves |
|---|---|---|---|---|---|---|
| <a business outcome, not a feature> | <persona> | <how it is measured> | <today, or "not tracked today"> | <number> | <date or window> | <the release test or objective it serves> |

## Non-Goals
| Item | Category | Reason | Revisit |
|---|---|---|---|
| <what this release does not do> | <later release / removed by decision / out of scope> | <why, citing the decision or source> | <when, or "never"> |
```

Cover every persona in Personas & Permissions and every objective the requester named. Goals
are outcomes the release must move (planning time, lateness, data lag), each traceable to a
release test, not a restatement of the features. Keep Lite PRDs in the short form.

## Rules

- **Requirements carry the request forward.** Refinery, work orders and the coding agent copy Requirements and Technical Constraints verbatim. Every named item, number, interval, limit and technology the requester stated appears there exactly, including times of day and fallbacks ("Friday 15:00 in the contractor's time zone", "email when there is no Slack account"). Mentioning them only in a flow is not enough; the runtime checks that each stated time and limit appears in Requirements, Technical Constraints or the Definition of Done.
- **Write Requirements last.** Read every other section back and give each behaviour, state, setting, error case and permission an FR row. A behaviour with no FR is an orphan; an FR with no `Specified in` is unspecified; one with no `Verified by` will ship untested.
- **In a Lite PRD**, `Specified in` points only at Proposed Solution, Key User Flows or Personas & Permissions. If an FR needs a Full-only section, re-check the triggers.
- **Every FR has a priority:** Must (the release fails without it), Should (expected, can slip
  one release) or Could (only if cheap). Jira orders stories by it and the first release is the
  Musts.
- **Decisions and Assumptions keep the reviewer honest.** Every intake answer that shaped the PRD
  is a Decision, citing the question. Every gap you filled without an answer is an Assumption,
  never a silent choice. Write `N/A: <reason>` when there are none.
- **Describe behaviour, not code.** No file paths, function names or code snippets; they go
  stale and the coding agent finds them in the repository. Name modules or systems only when the
  requester did.
- **Ids are append-only.** Never renumber or reuse `FR-n` or `DoD-n`. Withdraw in place: `FR-3: ~~withdrawn, superseded by FR-9~~`.
- **No Given/When/Then in the PRD.** Acceptance criteria are written per story when the PRD is filed in Jira.
- Definition of Done has 5 to 10 items. Every metric has a baseline and a target, or says why there is none.
- No PII, credentials or confidential customer names. Status is always `Draft`; only a human approves.
- Tool, memory or recall problems go in `warnings`, never in the PRD or anywhere the requester reads.

## Self-check before returning

- Profile matches the triggers; every required section is present or `N/A: <reason>`.
- Every stated requirement and technology appears in Requirements and Technical Constraints.
- Every FR has a priority, `Specified in` and `Verified by`; no orphan behaviour.
- Every intake answer appears under Decisions; every unanswered gap appears under Assumptions.
- No `<placeholder>`, TBD or TODO is left, and no vague word ("fast", "easy", "intuitive",
  "user-friendly", "seamless") stands without a number or a concrete behaviour.
- Non-Goals is not empty; every open question has an owner.
- In a Full PRD, the Problem Statement has its persona table, Current state, Cost of doing
  nothing and Why now; every goal has a measure, baseline and target; every non-goal a reason.

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

The runtime runs the same checks on `result.prd_markdown` and shows anything it finds to the
reviewer next to the draft, so fix them before returning.

Governed `prd_request`: return the PRD in `result.prd_markdown` only; the runtime publishes it
to Google Drive. Outside a workflow, follow `skills/pm/google-workspace.md` to save a Doc, and
continue with Markdown only if that fails.

## Design references

When `input.screen_specs` is present, it describes each Figma screen (`SC1`…: purpose, layout,
data shown, actions, states drawn) and its questions (`S1`…). The requester's answers are in
`input.clarified_requirements`, by S-number; a question they did not answer takes its
recommended answer, and a correction they wrote ("SC3: …") wins over the spec. Build each
screen's requirements and Given/When/Then criteria from the spec plus those answers, citing the
screen and its frames ("SC3, per D12"), and include how each screen behaves on tablet and phone
when the answers say so.

When `input.design_refs` is present, the request came with Figma frames or images (`D1`, `D2`…); the images follow the task message and `input.design_refs_text` names them. Read them as the source for the user interface: describe the screens, content, states and flows they show in the requirements, cite them by id ("per D2"), and list questions where a design and the text disagree. Never invent UI the designs and text don't support.
