# Epic

Summary: the feature name. Issue type: `Epic`. Labels: `prd`, the component area.

```markdown
## Goal
<One paragraph: the outcome when every story is done.>

## Why now
<The driver: customer evidence, deadline, dependency or metric.>

## PRD
<Drive link to the approved PRD.>

## Requirements covered
| FR | Requirement | Stories |
|---|---|---|
| FR-1 | <short form> | <story titles, filled after filing> |

## Invariants
- AD-1: <a rule every story must keep, for example "audit every write">

## Scope
**In:** <...>
**Out:** <...>

## Definition of done
- DoD-1: <verifiable, customer-observable>

## Risks and open questions
| Question | Owner | Blocks |
|---|---|---|
| <...> | <named person or role> | <story title or "none"> |
```

Rules:

- The story list is the epic's child issues in Jira, not a section of the epic.
- Copy FR and DoD ids from the PRD unchanged; never renumber.
- Every FR in the PRD appears in Requirements covered. An FR with no story is listed with
  `none` in Stories and raised as an open question.
- Record later scope decisions as dated comments on the epic, leading with the behaviour in
  plain words.
