# Validator Templates

Validator produces drift reports, corrective Work Orders, and feedback processing records.

Use the canonical templates in this directory:

- Drift report template: `templates/validator/drift-report-template.md`
- Feedback ingestion template: `templates/validator/feedback-ingestion-template.md`
- Validator gates: `quality-gates/validator.md`

Production additions:

- Classify drift as implementation drift, spec drift, or intent drift.
- Cite both code evidence and spec evidence for every DRIFT item.
- Record major drift decisions in `docs/prd-to-work-orders/approvals.md`.
- Record drift rate and false-positive review in `docs/prd-to-work-orders/metrics.md`.
