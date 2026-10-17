# Failure Modes and Recovery

When the pipeline cannot proceed safely, stop with evidence and offer the narrowest recovery path.

## Failure Categories

| Category | Signal | Recovery |
|---|---|---|
| Missing input | No PRD, missing upstream artifacts, or no code for Validator. | Ask for the exact missing input. |
| Partial stage | Some artifacts exist but required outputs are missing. | Offer `resume` or `regenerate`. |
| Invalid artifact | Required headings, IDs, trace links, or manifest entries are missing. | Produce a repair list and ask `patch` or `regenerate`. |
| Stale downstream | Upstream source hash changed after downstream generation. | Show impacted stages and ask `patch`, `regenerate`, or `migrate`. |
| Conflicting docs | Terms, contracts, dependencies, or decisions contradict. | Route conflict to the owning stage. |
| User skip request | User asks to skip a required stage or gate. | Explain risk and require explicit confirmation. |
| External blocker | Hardware, legal, compliance, or platform information is unavailable. | Record `DEFER` with owner, risk, and review trigger. |

## Stop Message Format

```markdown
## Blocked: [Failure Category]

**Current state:** [Detected state]
**Missing or invalid:** [Exact file, gate, or decision]
**Why this blocks:** [One paragraph]
**Recovery options:** resume | patch | regenerate | migrate | defer
**Recommended option:** [One option with reason]
```

## Recovery Rules

- Never silently skip a gate.
- Never overwrite owned artifacts during recovery without explicit approval.
- Never fix upstream artifacts from a downstream stage.
- Record every recovery action in `docs/prd-to-work-orders/run-log.md`.
- If recovery creates an assumption, copy it into the manifest.
