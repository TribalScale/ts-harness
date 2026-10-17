# Stage: Refinery

Transform a raw PRD into structured, implementation-agnostic requirements.

## Inputs

- Raw PRD path or pasted PRD.
- Existing `docs/prd-to-work-orders/manifest.json`, if present.
- Any approved or deferred assumptions from prior runs.

## Outputs

```text
docs/refinery/
├── overview/
│   ├── business-problem.md
│   ├── current-state.md
│   ├── personas.md
│   ├── product-description.md
│   ├── success-metrics.md
│   └── technical-requirements.md
├── features/
│   ├── FRD-001-feature-name.md
│   └── ...
├── feature-tree.md
├── working-assumptions.md
├── phase-1-prd-analysis.md
└── phase-5-cross-frd-consistency.md
```

## Phases

1. **PRD analysis and gap detection** — separate requirements from technical content, identify gaps, contradictions, ambiguities, and assumptions.
2. **Gap approval or deferral** — use `approval-gates.md`; do not proceed without explicit approval or recorded deferral.
3. **Product overview authoring** — write business problem, current state, personas, product description, success metrics, and technical requirements.
4. **Feature decomposition** — propose feature tree with dependencies and complexity.
5. **Feature tree approval** — pause for approval, revision, or deferral.
6. **FRD authoring** — write one FRD per feature in dependency order.
7. **Cross-FRD consistency** — verify terminology, dependencies, traceability, and coverage.
8. **Manifest update** — write or update the `refinery` section of `docs/prd-to-work-orders/manifest.json`.

## Rules

- FRDs describe what and why, never how.
- Technical decisions belong in `overview/technical-requirements.md` and the feature tree handoff section.
- Acceptance criteria use `When [condition], the system shall [behavior]`.
- Every requirement traces to a persona and business problem.
- Every open assumption receives an `ASM-*` or `DEFER:*` marker and is copied into the manifest.

## Quality Gates

Run `quality-gates/refinery.md`. Refinery is complete only when:

- Every FRD passes gate checks.
- The feature tree has no circular dependencies.
- All terms used across multiple FRDs are in the shared terminology index.
- Every PRD requirements-bearing section maps to at least one FRD.
- Manifest `stages.refinery.status` is `complete`.
