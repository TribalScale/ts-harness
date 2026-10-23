# Stage: Foundry (Governed Runtime)

Transform Refinery JSON artifacts into technical Blueprints returned as JSON markdown.

## Inputs

- `input.approved_prd` — source PRD
- `input.prior_refinery` — refinery `output.artifacts` from `refine_requirements`
- `input.stage` — must be `foundry`

## Outputs (JSON artifacts) — ALL keys required

| Key | Content |
|---|---|
| `blueprints_markdown` | Container, component (`## COMP-NNN`), feature (`## FB-NNN`) blueprints |
| `architecture_decisions_markdown` | ADRs with six-section template per major decision (`## ADR-NNN`) |
| `architecture_summary_markdown` | Cross-blueprint consistency under `# Architecture Summary` |

Runtime validation requires **heading IDs** (not bullet mentions):

- At least two of: `## ADR-NNN`, `## FB-NNN`, `## COMP-NNN` (or `CB-`)
- A dedicated `# Architecture Summary` (or System Design / Architecture Overview) section

## Phases

1. **Container identification** — deployable units and container blueprints.
2. **Capability mining** — shared capabilities across FRDs.
3. **Blueprint authoring** — component blueprints first, then feature blueprints.
4. **Adversarial review** — challenge ADRs; rebut or revise.
5. **Architecture summary** — consistency check across blueprints.

## Required JSON example

```json
{
  "summary": "Foundry complete: COMP/FB blueprints and ADR-001/002 for checkers realtime + sessions.",
  "artifacts": {
    "architecture_decisions_markdown": "## ADR-001 Real-time Communication\n\n## Context\n...\n\n## Alternatives Considered\n...\n\n## Decision\nUse WebSockets.\n\n## Rejection Rationale\n...\n\n## Trade-off Scorecard\n...\n\n## Consequences and Downstream Impact\n...\n\n## ADR-002 Session Persistence\n\n## Context\n...\n\n## Alternatives Considered\n...\n\n## Decision\n...\n\n## Rejection Rationale\n...\n\n## Trade-off Scorecard\n...\n\n## Consequences and Downstream Impact\n...",
    "blueprints_markdown": "## COMP-001 Game Server\n\nOwns match state and turn validation.\n\n## FB-001 Matchmaking\n\nCovers queueing and private rooms for FRD-001.",
    "architecture_summary_markdown": "# Architecture Summary\n\nWebSocket game server plus token resume keep matches coherent. COMP-001 and FB-001 align with ADR-001/002."
  },
  "confidence": "high",
  "warnings": [],
  "follow_up_needed": false
}
```

## ADR Template (inline)

Each ADR in `architecture_decisions_markdown` must use a heading ID:

```markdown
## ADR-001 Title

## Context
...

## Alternatives Considered
...

## Decision
...

## Rejection Rationale
...

## Trade-off Scorecard
...

## Consequences and Downstream Impact
...
```

## Blueprint heading rules

```markdown
## COMP-001 Game Server

Owns ...

## FB-001 Matchmaking

Covers FRD-001 ...
```

Do **not** bury IDs only in bullets like `- **FB-001**: ...` without `## FB-001` headings.

## Rules

- Every FRD in `prior_refinery` gets exactly one Feature Blueprint (`## FB-NNN`).
- Feature Blueprints compose Component Blueprints (`## COMP-NNN`).
- Preserve traceability to `input.prior_refinery` FRD IDs.
- Do not silently edit FRD content; note upstream issues in `warnings`.
- Do not return an action plan, catalog list, or memory-recall summary as artifacts.

Apply `quality-gates/foundry-runtime.md` before returning JSON.
