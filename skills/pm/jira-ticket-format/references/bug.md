# Bug

Summary: what is broken, in user terms, 72 characters or fewer. Issue type: `Bug`
(or the tenant's configured type).

```markdown
## Steps to reproduce
1. <step>
2. <step>

## Expected
<What should happen.>

## Actual
<What happens instead.> Failure class: <crash | wrong result | false positive |
false negative | silent skip | data loss | performance | security>.

## Evidence
- `path/to/file.py:42`: <what the line does wrong>
- <log line, error message or screenshot description>

## Blast radius
<Who and what is affected: users, tenants, data, frequency.>

## Regression test
<The test that fails before the fix and passes after it.>

## Notes
- Diagnosis confidence: <high | medium | unknown cause>
- <workaround, related history>
```

Rules:

- Evidence cites only files the diagnosis actually read. An unknown cause is allowed;
  say so instead of guessing.
- Quote exact error messages.
- The regression test is required even when the cause is unknown; describe the failing
  behaviour it must pin down.
