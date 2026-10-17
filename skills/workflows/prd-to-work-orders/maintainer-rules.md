# Maintainer Rules

The umbrella skill must stay modular. If a change makes the skill harder to scan, move detail into the correct module instead of expanding `SKILL.md`.

## File Responsibilities

| File type | Responsibility |
|---|---|
| `SKILL.md` | Orchestration, state routing, global stop rules, links to modules. |
| `stages/*.md` | Stage-specific process and output rules. |
| `templates/**` | Artifact templates and examples. |
| `quality-gates/**` | Stage-specific gate checklists. |
| `schemas/**` | Machine-checkable contracts. |
| `examples/**` | Golden paths and fixtures. |
| `docs/**` | User-facing and maintainer-facing documentation. |

## Size Thresholds

- `SKILL.md`: keep under 250 lines.
- Stage modules: keep under 300 lines each.
- Quality-gate files: one stage per file.
- Template files: one artifact responsibility per file.

## Review Questions

Every skill update must answer:

1. Is this orchestration? Put it in `SKILL.md`.
2. Is this stage behavior? Put it in `stages/`.
3. Is this output shape? Put it in `templates/` or `schemas/`.
4. Is this validation? Put it in `quality-gates/`.
5. Is this an example or regression case? Put it in `examples/`.

## Release Checks

Before publishing a skill update:

- State machine still routes every valid state.
- Manifest schema version is correct.
- Stage modules reference the current quality gates.
- Golden path still represents the expected pipeline.
- Changelog entry exists.
