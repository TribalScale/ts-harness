# Story

Summary: a short title, 72 characters or fewer. Issue type: the project's story type
(`Story` unless the tenant config says otherwise). Labels: `prd`, the component area,
and `enabler` for the enabler variant.

## User-facing story (default)

````markdown
## Story
As a <persona>, I want <capability> so that <outcome>.

**Requirements:** FR-2, FR-5

## Fields affected
| Field | Source | Required, defaulted or proposed |
|---|---|---|
| <user-meaningful fields first> | <where the value comes from> | <...> |

## Interaction
Build against: <Figma frame, existing screen, or "new screen">.
- <explicit change from that base>

## Acceptance criteria
```gherkin
Scenario: <success path>
  Given <context>
  When <action>
  Then <observable outcome, exact strings quoted>

Scenario: <error | validation | permission | empty state>
  Given <context>
  When <action>
  Then <observable outcome>
```

## Tasks
- [ ] <build unit>

## Test cases
- [ ] TC-1 (positive): <what is checked>
- [ ] TC-2 (negative): <what is checked>

## Design references
| State | Frame |
|---|---|
| <state> | <Figma link> |

## Invariants
- AD-1: <only the epic invariants this story could break>

## Notes
- <what is new versus existing, what is explicitly not changing>

## Out of scope
- <plain-language exclusion; never name the story that owns it>
````

Filling rules:

- One persona line under 30 words. Combine personas unless permissions, steps,
  validations or outcomes differ.
- Skip Fields affected when the Story line already pins the data.
- One scenario per state, not per screen. Every scenario has a Given, a When and a Then.
- With no Figma frame, replace the table with one line per state:
  `**<State>**: no Figma frame. The change is: <one sentence>.`
- Probabilistic behaviour (what an AI proposes, when it abstains) goes in an
  `## Eval criteria` section as targets plus a few reference cases, not in scenarios.
- Omit Invariants, Notes or Out of scope when they would be empty. Keep Design
  references on UI stories even when it only says there is no frame.

## Technical story

A system outcome with no persona. Replace `## Story` with:

```markdown
## Outcome
<What the system does after this ships, in one or two sentences.>

**Requirements:** FR-4
```

Keep Acceptance criteria (scenarios against the system or API), Tasks and Test cases.
Its build units may be filed as separate Jira Tasks when they are independently shippable.

## Enabler story

Backend work that exists to unblock one user-facing story. Label `enabler`.

```markdown
## Outcome
<The capability this makes available.>

## Contract
<Endpoint, event or interface: inputs, outputs, errors, limits.>

Consumed by: <the user-facing story's title>

## Acceptance criteria
<API-level scenarios: success, validation error, permission denied.>

## Tasks
- [ ] <build unit>
```

File it with a `Blocks` link where the enabler is the outward (blocking) issue and the
story it unblocks is the inward issue.
