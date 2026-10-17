# Versioning and Compatibility

The skill and its artifacts are versioned separately so old outputs can be evaluated, migrated, or intentionally regenerated.

## Versioned Surfaces

| Surface | Field | Meaning |
|---|---|---|
| Skill | `skillVersion` | Orchestration behavior and global policy. |
| Manifest schema | `schemaVersion` | Shape of `docs/prd-to-work-orders/manifest.json`. |
| Stage module | `stages.<stage>.version` | Stage behavior used to generate artifacts. |
| Artifact set | `stages.<stage>.artifacts[].version` | Schema version of generated artifacts. |

## Semantic Versioning

- Patch: wording fixes, examples, and clarifications without schema changes.
- Minor: new optional fields, optional gates, or backwards-compatible behavior.
- Major: required schema changes, artifact layout changes, or ID semantics changes.

## Compatibility Rules

- Compatible manifest upgrades may migrate automatically and record lineage.
- Incompatible artifact upgrades require user approval.
- Major version changes require a migration report.
- Old outputs remain readable; they may simply fail newer quality gates until migrated.

## Migration Report

Write migration reports to:

```text
docs/prd-to-work-orders/migrations/YYYY-MM-DD-<from>-to-<to>.md
```

Each report must include:

- Source version
- Target version
- Artifacts changed
- Required headings added
- Deprecated fields
- ID changes
- Downstream stages affected
- Verification performed
