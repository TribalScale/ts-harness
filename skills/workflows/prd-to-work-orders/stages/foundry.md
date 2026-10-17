# Stage: Foundry

Transform Refinery requirements into technical Blueprints. Foundry defines how the system is built while preserving traceability to what the FRDs require.

## Inputs

- Complete `docs/refinery/**`.
- `docs/prd-to-work-orders/manifest.json` with `refinery.status = complete`.
- Approved or deferred assumptions.

## Outputs

```text
docs/foundry/
├── containers/
├── components/
├── features/
├── architectural-challenges.md
├── architecture-summary.md
└── foundry-issues.md
```

## Phases

1. **Container identification** — identify deployable units and write Container Blueprints.
2. **Capability mining and proposal** — identify shared capabilities across FRDs.
3. **Capability map approval** — pause for human approval before Blueprint authoring.
4. **Blueprint authoring** — write Component Blueprints first, then Feature Blueprints.
5. **Adversarial architecture review** — use `templates/foundry/adversarial-review-prompt.md`; every ADR challenge is rebutted or revised.
6. **Cross-Blueprint consistency** — run Blueprint gates and produce architecture summary.
7. **Manifest update** — write or update the `foundry` section.

## ADR Requirements

Every Blueprint ADR uses the six-section template:

1. Context
2. Alternatives Considered
3. Decision
4. Rejection Rationale
5. Trade-off Scorecard
6. Consequences and Downstream Impact

The scorecard includes quantified latency/performance, cost, complexity, team skill match, reversibility, and full Agent Implementability breakdown for the chosen option and every alternative.

## Rules

- Every FRD gets exactly one Feature Blueprint.
- Feature Blueprints compose Component Blueprints; they do not duplicate shared capability definitions.
- Component Blueprints are feature-agnostic.
- Container Blueprints describe runtime boundaries and cross-cutting concerns.
- Data models include full fields, constraints, relationships, and invariants.
- Contracts include request, response, error, and event payload shapes.
- Foundry never silently edits FRDs. Use `foundry-issues.md` for upstream issues.

## Quality Gates

Run `quality-gates/foundry.md`. Foundry is complete only when:

- Every FRD has a Feature Blueprint.
- Every Blueprint has complete contracts and data models.
- Every ADR passes alternatives, rationale, scorecard, and consequence gates.
- `architectural-challenges.md` has no unresolved challenges.
- Manifest `stages.foundry.status` is `complete`.
