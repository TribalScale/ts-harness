# Fixture PRD: Regulated Child Data

## Purpose

This fixture verifies approval gates, assumptions, and compliance traceability.

## PRD

Build an app where parents create profiles for children under 13 and save the child's name, age, and interests. The app must comply with child privacy requirements. The legal team has not selected the parental consent method.

## Expected Behavior

- Refinery flags consent method as a gap.
- Refinery does not block behavioral FRDs if the gap is explicitly deferred.
- Manifest records the open assumption and owner.
- Validator later treats missing consent enforcement as implementation drift.
