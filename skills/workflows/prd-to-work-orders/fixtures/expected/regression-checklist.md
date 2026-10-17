# Regression Fixture Checklist

Run this checklist after changing the `prd-to-work-orders` skill.

## Team Tasks

- [ ] One FRD is produced.
- [ ] One Feature Blueprint is produced.
- [ ] One Work Order is produced.
- [ ] Validator reports clean implementation.

## Ambiguous Hardware

- [ ] Protocol ambiguity is recorded as a technical gap.
- [ ] FRDs remain protocol-agnostic.
- [ ] Foundry handoff preserves BLE/Wi-Fi candidates.

## Regulated Child Data

- [ ] Consent method gap is explicit.
- [ ] Open assumption appears in the manifest.
- [ ] Consent enforcement is traceable from FRD to Validator.

## Magic Mailbox Vertical Slice

- [ ] Foundry ADR includes real alternatives and Agent Implementability scoring.
- [ ] Planner Work Order includes consent-gate test coverage.
- [ ] Validator finds `DRIFT-001`.
- [ ] CWO-001 maps back to the source drift and Work Order.
