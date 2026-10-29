# Task

Technical work with no user-facing behaviour: migrations, infrastructure, tooling,
spikes. Issue type: `Task` (or `Spike` for a time-boxed investigation).

```markdown
## Overview
<What changes, in two or three sentences.> No underlying functionality is changing
outside the items listed below.

**Requirements:** FR-6

## Reference material
- <links or file paths a builder needs>

## What's changing
### <Group 1>
1. <change>
2. <removal, stated as work>

### <Group 2>
1. <change>

## Tasks
- [ ] <build unit>

## Acceptance criteria
- <observable check, with hard numbers>

## Test cases
- [ ] TC-1 (positive): <...>
- [ ] TC-2 (negative): <...>

## Out of scope
- <...>

## Open questions
- <question> (owner: <name or role>)
```

Rules:

- Use the scope-fence sentence in Overview unless behaviour does change; then say what.
- Number the changes inside each group so reviewers can cite them.
- If the scope is not known yet, the description is `Scope: TBD` plus Open questions.
  Do not invent acceptance criteria.
