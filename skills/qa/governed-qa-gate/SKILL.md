---
name: governed-qa-gate
description: Governed QA gate that verifies delivered PRs against acceptance criteria before the human merge wait. Use for implement_work_orders / implement_delivery qa_gate stage.
type: workflow
category: governed-workflows
allowed_agents: [qa-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.2.0
---

# Governed QA Gate

`governed-qa-gate` is the program stage that runs **after** delivery produces
PRs, their CI checks finish (`await_pr_checks`) and the automated code review
passes (`finalize_code_review`), and **before** `await_human_merge`. It confirms each delivered PR satisfies
its work-item acceptance criteria and emits the canonical QA envelope the
runtime uses to arm the merge wait (`match_all` over the PR set).

It is **not** the host verify step. Host verify (build/lint/typecheck/tests run
by `run_coding_backend` during `deliver_work_order`) proves the change compiles
and the delivered suite passes on the host. `qa_gate` is an independent
acceptance review of the resulting PR(s). Do **not** re-run the full test suite
by default — reuse the host verify summary as evidence and add targeted checks
only where acceptance criteria demand it.

## Start Here

At invocation, announce:

> "I'm running the governed QA gate over the delivered PR set."

Then:

1. Read `input.pr_urls` (Jira multi-PR path) or `input.pr_url` (single WO path).
   The set of PR URLs you report back **must equal** the input set exactly — no
   extras, no omissions. A mismatch blocks the gate.
2. Read `input.checks_state`, `input.checks` / `input.pr_checks`, and
   `input.failed_checks`. These are the repo's own CI results on the PR head
   SHA, collected by `await_pr_checks`. They are the primary build evidence.
   If `checks_state` is `failure`, the PR whose check failed is `failed` and
   each failed check is listed in `defects`.
3. Read `input.work_order_body` / work-item references, `input.files`, and
   `input.host_verify_summary`. `input.human_change_requests` lists changes a
   human reviewer requested on this PR, oldest first. They amend the work
   order's acceptance criteria: where one conflicts with the work order or an
   earlier request, the latest request wins, and meeting it is not a defect. Each `input.files` entry is one changed file
   (`filename`, `patch`, `additions`, `deletions`). That patch is the
   implementation. Judge each acceptance criterion from the patch. A missing
   `host_verify_summary` is not a defect and is not a reason to fail: host
   verify is supporting evidence only, and it never overrides a failed
   external check. When `checks_state` is `success`, cite those CI checks as
   the test evidence. Do not ask for a diff, a changed-file list, or a local
   workspace checkout that was not supplied. `input.host_security` is the
   host secret scan and dependency audit result (file and line only); cite it
   as security evidence.
4. For each PR, evaluate acceptance criteria and record evidence:
   - `acceptance_criteria`: per-criterion pass/fail with a short note.
   - `tests`: tests exercised or the host verify summary reused as evidence.
   - `defects`: blocking issues found (empty when the PR passes).
5. Set each PR `status` to `success` or `failed`.
6. Set top-level `status` to `success` **only if every** per-PR `status` is
   `success`; otherwise `failed`.
7. Return the canonical QA envelope exactly (see below).

## Output Contract (canonical QA envelope)

```json
{
  "status": "success",
  "result": {
    "summary": "All delivered PRs meet acceptance criteria.",
    "prs": [
      {
        "pr_url": "https://github.com/org/repo/pull/123",
        "work_item_ref": "WO-001",
        "status": "success",
        "acceptance_criteria": [
          { "criterion": "Endpoint returns 200", "status": "pass", "note": "verified in PR diff + host tests" }
        ],
        "tests": ["host verify: 42 passed"],
        "defects": [],
        "summary": "Meets all AC."
      }
    ]
  },
  "confidence": 0.9,
  "warnings": [],
  "follow_up_needed": false
}
```

Rules enforced by the `validate_qa_pr_url_set` semantic validator:

- Output `result.prs[].pr_url` set **==** input PR URL set, exactly.
- Top-level `status: success` requires **every** per-PR `status == success`.
- Schema-valid `status: failed` is a legitimate result and produces a **blocked**
  stage outcome (the merge wait is not armed). Do not fabricate success to pass.

## Failure Behavior

- A `failed` envelope blocks the workflow at `qa_gate`; `await_human_merge` never
  arms, so no external merge wait is left dangling. Metrics increment
  `qa_gate_failed`.
- A blocked gate sends the PR back to the dev agent automatically with your
  defects as `qa_feedback`. The revised PR then runs checks, code review and QA
  again. Code review and QA share a budget of 3 revisions per workflow; after
  that the stage stays blocked and a human decides.

## Not In Scope

- Merging PRs (humans merge; the orchestrator resumes `await_human_merge`).
- Re-running the full delivery/build pipeline.
- Editing code or opening new PRs.
