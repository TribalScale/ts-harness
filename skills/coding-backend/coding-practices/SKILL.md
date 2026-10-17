---
name: coding-practices
description: Mandatory TDD, verification, and root-cause debugging for governed work-order implementation. Use whenever implementing WO-NNN via OpenCode.
license: Apache-2.0
compatibility: opencode
metadata:
  audience: coding-backend
  owner: ts-harness
---

# Coding practices

Follow these practices for every governed work-order implementation. This run is **non-interactive**: implement immediately. Do not ask for approval, present a design for sign-off, or wait for a human. Specs under `docs/` are already approved.

The host coding backend owns git/gh. Do **not** commit, push, or open a pull request.

## Test-driven development

**Core principle:** If you didn't watch the test fail, you don't know if it tests the right thing.

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

For each behavior slice in this work order:

1. **RED** — write one failing unit or integration test that names the behavior.
2. Run it. Confirm it fails for the **right** reason (missing behavior, not a syntax/import error).
3. **GREEN** — write the minimal production code to pass.
4. **REFACTOR** — clean up with tests still green.

Do **not** write production code before a failing test for that slice. If you already wrote implementation first, delete it and start from the test. Do not keep it as "reference" or "adapt" it while writing tests.

**Always** for new features, bug fixes, and behavior changes. Skip only for throwaway config/docs-only chores with no observable behavior.

Thinking "skip TDD just this once"? Stop. That's rationalization.

RED tests must:

- Cover one behavior with a clear name
- Exercise real code (mocks only when unavoidable)
- Fail because the behavior is missing, not because the test is broken

Installing dependencies or fixing test runners is **not** implementation. After runners work, still deliver this work order's behavior with tests.

## Verification before claiming done

**Core principle:** Evidence before claims, always.

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

Before saying the work order is implemented, tests pass, or a bug is fixed:

1. Identify the command that proves the claim (repo test runner, BDD runner).
2. Run the **full** command in this session.
3. Read the output and exit code.
4. Only then state the result **with** that evidence.

Not sufficient: previous runs, "should pass", "looks correct", "I'm confident", partial checks.

The host will also run verification after you finish. Your job is still to leave a green, WO-scoped suite — not to assume the host will catch gaps.

## Debug failures before patching

When tests fail, verification fails, or behavior is wrong:

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

1. Read the full error, stack, and command output.
2. Reproduce it.
3. Check what changed (this WO vs pre-existing suite).
4. Fix the **cause**, then re-run the same command.

Do not paper over missing WO behavior by only adding packages, ignoring tests, or making an unrelated suite green. If host verification complains about missing features/code for this WO, implement that WO — do not stop at runner/deps.

## Repo conventions

- Use the target repo’s existing test runner, layout, and linters.
- Prefer small, focused diffs that stay inside the work-order scope.
- Do not invent a second test stack when one already exists.

## Done when

- Each in-scope behavior had a failing test first, then passing code (TDD).
- Fresh test/BDD output in this session is green for the change set.
- You stated results with that evidence, not with "should work".
