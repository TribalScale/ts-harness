# Stage Gate Upgrade Summary

The production-grade gates extend the legacy stage gates with risks observed in the Magic Mailbox Refinery output and the vertical-slice proof.

## Refinery Upgrades

Added gates for:

- Gap disposition before downstream work.
- Manifest freshness.
- Artifact ownership.
- Full PRD coverage and dependency correctness.

Why: Magic Mailbox proceeded through Refinery with many deferred assumptions. That is acceptable only if the assumptions are visible in the manifest and approvals.

## Foundry Upgrades

Added gates for:

- Six-section ADRs.
- Minimum real alternatives.
- Concrete rejection rationale.
- Quantified scorecards.
- Agent Implementability for each alternative.
- Resolved adversarial challenges.
- Ownership and manifest freshness.

Why: downstream Work Orders inherit architectural weaknesses directly. Foundry needs explicit challenge and trade-off mechanics before Planner runs.

## Planner Upgrades

Added gates for:

- Work Order executability.
- Local testability.
- Context graph symmetry.
- Contract ownership.
- Ownership and manifest freshness.

Why: a Work Order is not production-ready unless a fresh coding agent can execute it without additional product or architecture discovery.

## Validator Upgrades

Added gates for:

- Drift classification into implementation, spec, and intent drift.
- False-positive review.
- CWO minimal scope.
- Feedback deduplication.
- Pipeline continuity back to Planner.
- Ownership and manifest freshness.

Why: Validator should not blindly prefer specs over code. It must preserve evidence and route corrections through traceable Work Orders.
