# Coding-backend skills

Implementation skills for the configured coding backend (`implementation.coding_backend`).

These are **not** TS Harness agent skills. They are copied into the coding
tool's skill directory (for OpenCode: `.opencode/skills/`) so the coding agent
can load them while implementing work orders.

## Skills

| Skill | Role |
|---|---|
| `implement-work-order` | Orchestration: read WO + docs, stay in scope, host owns git/gh |
| `coding-practices` | TDD, verification-before-done, and root-cause debugging |
| `bdd-acceptance` | Docs/WO-derived Gherkin acceptance automation within WO scope |

`implement-work-order` requires loading `coding-practices` then `bdd-acceptance` after reading specs.

Practice rules live in these three skills. Do not install extra OpenCode plugins for TDD/debug
workflows — they pull in human-approval planning that stalls unattended delivery.

## Install paths

- Source of truth: this directory
- Install at setup: `scripts/phases/11h-coding-tools-install.sh` mirrors into tools root
- Materialize per implement: `run_coding_backend` copies into the app clone before the coding tool runs
- Per-clone OpenCode config: planning plugins are stripped from `.opencode/opencode.json` so only these skills apply
