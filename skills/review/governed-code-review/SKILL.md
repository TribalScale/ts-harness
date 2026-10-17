---
name: governed-code-review
description: Governed code review of delivered PRs against the work order before the QA gate. Use for the verified_merge code_review stage.
type: workflow
category: governed-workflows
allowed_agents: [review-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.2.0
---

# Governed Code Review

`governed-code-review` runs in the `code_review` stage, after the PR's CI checks
finish (`await_pr_checks`) and `review_precheck` has collected the diff, and
before `qa_gate`. You review the diff and return findings. You do not decide
whether the PR passes: `finalize_code_review` computes the verdict from your
findings, posts one `COMMENT` review with inline comments, and sets the
`harness/code-review` status.

You never approve a PR, never request changes, never merge, and never edit code.

## Start Here

At invocation, announce:

> "I'm running the governed code review over the delivered PR diff."

Then:

1. Read `input.prs` (PR URL and head SHA). The PR set you report back **must
   equal** this set exactly.
2. Read `input.files`: each changed file with `filename`, `status`,
   `additions`, `deletions`, `patch` (truncated at 20k characters; see
   `patch_truncated`) and `pr_url`. Lock files and generated code may already
   be excluded; `input.diff_stats` has the totals.
3. Read `input.work_order_body` and `input.work_order_ref`. Judge scope against
   its **In Scope** and **Out of Scope** sections (and a `## Files` list when
   present).
4. Read `input.scope_findings`. These are deterministic findings for files
   outside a declared `## Files` list; they are merged automatically, so do
   not repeat them.
5. If `input.refresh` is `true`, the PR was just updated with its base branch.
   Focus on the merge delta and any conflict resolution; do not re-review code
   that was already reviewed unless the merge changed it.
6. Trace every bullet under **Requirements** and every **Acceptance Test** to
   the code that implements it. Evaluate the code by hand with the example
   values the work order gives, including each boundary it names ("100 or
   more", "exactly 50 units"). A mismatch is a `major` `correctness` finding
   even when CI passes: CI only runs the tests the PR wrote.
7. If the work order contradicts itself, Requirements and Acceptance Tests win
   over Implementation Notes. Code that follows a contradicting note is still a
   `correctness` finding; say which lines of the work order conflict.
8. Apply the checklist below to every changed file and return the envelope.

## Review Checklist

| Category | What to check |
| --- | --- |
| `correctness` | Logic matches the work order's requirements, edge cases, null safety, error handling, off-by-one, concurrency |
| `security` | No secrets, input validation, injection, authn/authz checks, unsafe deserialization, OWASP top 10 |
| `scope` | Changes stay inside In Scope; nothing from Out of Scope; no unrelated refactors |
| `tests` | New behaviour has meaningful tests; assertions check outcomes; no flaky patterns |
| `maintainability` | Naming, dead code, duplication, following the repo's existing patterns |

## Severity

| Severity | Use for |
| --- | --- |
| `blocker` | Broken functionality, data loss, a security hole, or a change that cannot ship |
| `major` | A real defect or risk that must be fixed before merge |
| `minor` | Worth fixing, but not a merge risk |
| `nit` | Style or preference |

A `blocker` in any category, or a `major` in `correctness`, `security` or
`scope`, blocks the PR and sends it back to the dev agent with your findings.

Calibrate `correctness` against the work order, not your preferences:

- Code that gives a wrong result for a case the work order states (a
  requirement, an acceptance test, a named boundary) is at least `major`
  `correctness`, never a `tests` finding. Name the input and the expected and
  actual result.
- Hardening the work order does not ask for ("consider validating negative
  quantities") is `minor` at most. "Consider ..." is never `major`.
Be specific: every finding needs a `message` a developer can act on, and a
`file` and `line` (a line in the new version of the file) when it is about a
particular place.

## Output Contract

```json
{
  "result": {
    "verdict": "pass",
    "summary": "Implements the endpoint as specified; one missing edge-case test.",
    "prs": [{ "pr_url": "https://github.com/org/repo/pull/123", "head_sha": "abc123" }],
    "findings": [
      {
        "severity": "minor",
        "category": "tests",
        "pr_url": "https://github.com/org/repo/pull/123",
        "file": "src/api/orders.py",
        "line": 42,
        "message": "Add a test for an empty cart; the new branch at line 42 is untested."
      }
    ]
  }
}
```

Rules enforced by the `validate_code_review_envelope` semantic validator:

- `result.prs[].pr_url` set **==** the input PR set, exactly.
- Every finding's `pr_url` is one of the input PRs.
- `severity` is one of `blocker`, `major`, `minor`, `nit`; `category` is one of
  `correctness`, `security`, `scope`, `tests`, `maintainability`.
- `verdict` is your opinion (`pass` or `block`). The computed verdict wins; a
  mismatch is recorded as a warning.

An empty `findings` list is a valid, passing review. Do not invent findings to
look thorough, and do not hide real ones to pass.

## Not In Scope

- Approving, requesting changes on, or merging PRs.
- Editing code, pushing commits, or opening PRs.
- Re-running CI or the test suite (CI results are already in `await_pr_checks`).
