# Stage: Foundry (Governed Runtime)

Transform Refinery JSON artifacts into technical Blueprints returned as JSON markdown.

## Inputs

- `input.approved_prd` — source PRD
- `input.prior_refinery` — refinery `output.artifacts` from `refine_requirements`
- `input.stage` — must be `foundry`
- `input.project_repositories` — the project's repositories, when it has several; name the repository each blueprint belongs to

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

Write prose in every section. A section body that is only `...` is invalid and will be rejected. Do not copy a skeleton.

```json
{
  "summary": "Foundry complete: COMP/FB blueprints and ADR-001/002 for checkers realtime + sessions.",
  "artifacts": {
    "architecture_decisions_markdown": "## ADR-001 Real-time Communication\n\n## Context\nPlayers need move updates within one turn window.\n\n## Alternatives Considered\nWebSockets versus long polling.\n\n## Decision\nUse WebSockets for live match updates.\n\n## Rejection Rationale\nLong polling adds a full round trip per move and misses the latency budget.\n\n## Trade-off Scorecard\nWebSockets cost connection management and gain sub-second updates.\n\n## Consequences and Downstream Impact\nClients must reconnect and resume from the last acknowledged move.\n\n## ADR-002 Session Persistence\n\n## Context\nA dropped connection must not forfeit an in-progress match.\n\n## Alternatives Considered\nSticky sessions versus a resume token.\n\n## Decision\nIssue a resume token stored with the match.\n\n## Rejection Rationale\nSticky sessions fail when the holding server restarts.\n\n## Trade-off Scorecard\nTokens add a lookup and remove server affinity.\n\n## Consequences and Downstream Impact\nThe match service validates the token before replaying missed moves.",
    "blueprints_markdown": "## COMP-001 Game Server\n\nOwns match state and turn validation.\n\n## FB-001 Matchmaking\n\nCovers queueing and private rooms for FRD-001.",
    "architecture_summary_markdown": "# Architecture Summary\n\nWebSocket game server plus token resume keep matches coherent. COMP-001 and FB-001 align with ADR-001/002."
  },
  "confidence": "high",
  "warnings": [],
  "follow_up_needed": false
}
```

## ADR Template (inline)

Each ADR in `architecture_decisions_markdown` must use a heading ID and a prose body under every section. Do not leave a section as `...`.

```markdown
## ADR-001 Title

## Context
What constraint forces this decision.

## Alternatives Considered
The options that were compared.

## Decision
The option selected.

## Rejection Rationale
Why the other options were rejected.

## Trade-off Scorecard
What the selected option costs and what it gains.

## Consequences and Downstream Impact
What later blueprints and work orders must do because of this decision.
```

## Blueprint heading rules

```markdown
## COMP-001 Game Server

Owns match state and turn validation.

## FB-001 Matchmaking

Covers queueing and private rooms for FRD-001.
```

Do **not** bury IDs only in bullets like `- **FB-001**: matchmaking` without a `## FB-001` heading.

## Rules

- Every FRD in `prior_refinery` gets exactly one Feature Blueprint (`## FB-NNN`).
- Feature Blueprints compose Component Blueprints (`## COMP-NNN`).
- Preserve traceability to `input.prior_refinery` FRD IDs.
- Design inside the Technical Constraints from `input.prior_refinery` and the
  PRD. A stated framework or storage choice is fixed; ADRs decide only what the
  constraints leave open.
- Do not silently edit FRD content; note upstream issues in `warnings`.
- Do not return an action plan, catalog list, or memory-recall summary as artifacts.

Apply `quality-gates/foundry-runtime.md` before returning JSON.
