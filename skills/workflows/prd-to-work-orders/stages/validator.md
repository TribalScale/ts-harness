# Stage: Validator

Compare implemented code against Planner Work Orders, Foundry Blueprints, and Refinery FRDs. Produce evidence-based drift reports and corrective Work Orders.

## Inputs

- Complete `docs/refinery/**`.
- Complete `docs/foundry/**`.
- Complete `docs/planner/**`.
- Implemented code for the declared scope.
- Test, lint, or review evidence where available.

## Outputs

```text
docs/validator/
├── drift-reports/
├── corrective-work-orders/
└── feedback/
```

## Functions

1. **Drift detection** — compare data models, contracts, acceptance criteria, boundaries, names, and ADRs.
2. **Drift classification** — minor drift generates CWOs; major drift escalates with fix-code and update-spec options.
3. **Feedback ingestion** — map user feedback and test results to specs through the context graph.
4. **Manifest update** — write or update the `validator` section.

## Drift Types

| Type | Meaning | Default action |
|---|---|---|
| Implementation drift | Code fails to satisfy the agreed spec. | Generate CWO or fix-code recommendation. |
| Spec drift | Code appears valid but spec is obsolete or incomplete. | Escalate update-spec option. |
| Intent drift | Product intent changed after artifacts were generated. | Reopen owning upstream stage. |

## Evidence Rules

Every DRIFT item cites:

- Code path and line or symbol.
- Spec source: FRD requirement, Blueprint section, or Work Order.
- Impact.
- Classification rationale.
- Corrective path.

Validator must not fix code directly. All corrections flow through CWOs or explicit user-approved spec updates.

## Quality Gates

Run `quality-gates/validator.md`. Validator is complete only when:

- Every drift item has evidence.
- Minor drift has a CWO.
- Major drift has a human decision.
- Feedback items are classified as spec gap, implementation gap, or enhancement.
- Manifest `stages.validator.status` is `complete`.
