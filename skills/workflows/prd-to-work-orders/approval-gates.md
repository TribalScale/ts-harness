# Human Approval Gates

The umbrella skill centralizes human decisions. Stage modules may prepare recommendations, but progression pauses at mandatory gates unless the user approves or explicitly defers.

## Required Gates

| Gate | Stage | Blocks | Decision options |
|---|---|---|---|
| Gap Resolution | Refinery | Product overview and FRD authoring | Approve, resolve, defer |
| Feature Tree Approval | Refinery | FRD authoring | Approve, revise, defer |
| Capability Map Approval | Foundry | Blueprint authoring | Approve, revise |
| Execution Phase Approval | Planner | Coding-agent handoff | Approve, revise, defer |
| Major Drift Decision | Validator | Corrective work | Fix code, update specs, defer |

## Decision Record

Approval records live in `docs/prd-to-work-orders/approvals.md`.

```markdown
## APPROVAL-NNN: [Checkpoint Name]

**Date:** YYYY-MM-DD
**Stage:** Refinery | Foundry | Planner | Validator
**Decision:** Approved | Deferred | Rejected | Revise
**Summary:** [One paragraph]
**Open Assumptions:** [ASM IDs or none]
**Downstream Impact:** [What this unlocks or blocks]
```

## Deferral Rules

Deferred gates must include:

- Owner
- Risk
- Review trigger
- Downstream artifacts allowed to proceed
- Downstream artifacts blocked

Deferrals must be copied into the manifest so downstream stages cannot miss them.

## Gate Enforcement

- Do not continue from Refinery gap analysis to FRD authoring without gap approval or deferral.
- Do not write Foundry Blueprints until the capability map is approved.
- Do not hand Work Orders to coding agents until execution phases are approved or explicitly deferred.
- Do not auto-resolve major drift. Present fix-code and update-spec options.
