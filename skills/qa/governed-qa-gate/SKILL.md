---
name: governed-qa-gate
description: Governed QA gate that verifies delivered PRs against acceptance criteria before the human merge wait. Use for implement_work_orders / implement_delivery qa_gate stage.
type: workflow
category: governed-workflows
allowed_agents: [qa-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.11.0
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
   implementation. Judge each acceptance criterion from the patch. Each
   `input.prs` entry includes `title`, the pull request title on GitHub.
   Judge a title criterion, including a Jira-key prefix, from that field. A
   title that does not meet the criterion is a defect. A missing
   `host_verify_summary` is not a defect and is not a reason to fail: host
   verify is supporting evidence only, and it never overrides a failed
   external check. When `checks_state` is `success`, cite those CI checks as
   the test evidence. Do not ask for a diff, a changed-file list, or a local
   workspace checkout that was not supplied. `input.host_security` is the
   host secret scan and dependency audit result (file and line only); cite it
   as security evidence.
   `input.host_verify_summary` lists each command the host ran on the final
   change (tests, type check, lint) with its exit code and the end of its
   output; cite it as test evidence alongside CI. It includes the
   `integration` suite the host ran against real databases when the repo has
   one, and, when the work order mentions coverage, the overall line coverage
   and each changed file's: judge coverage criteria against those numbers
   (a file "not loaded by any test" is 0%).
   For a frontend change, `input.screenshots` names the screenshots attached
   after the task message, from the Playwright tests the change added or
   edited: `step-N` is the page as one of the test's assertions passed, a name
   without a step is the end of the test (often after cleanup). Look at them for user-visible criteria (text, labels,
   empty states, layout that is clearly broken) and cite them by name. A
   screenshot shows one moment; judge behaviour over time (debounce, live
   updates) from the tests. No screenshots for a UI criterion is
   `unverified`, not a defect.
   **Compare against the design: every listed frame.** When `input.design_refs`
   is present, the design images (labelled `Design D1: …`) follow the
   screenshots. Each design reference the work order lists is its own
   criterion, `Design D1: <frame title>`, judged against its capture:
   - `input.frame_captures` holds, per frame, a screenshot the harness took at
     the frame's own size in the state the work order's frame checklist names
     (labelled `Capture D1: …`), or why it could not be taken. Compare each
     frame with its capture, region by region: the panels, lanes, rows, cards,
     badges, controls and copy the frame shows, and the states it draws
     (selected chip, open panel, dimmed blocks, expanded rows). A missing
     element, panel or state, missing card content (a status line, a time
     left, a progress bar), or a broken layout is a defect citing both images.
   - `input.design_checks` lists, per frame and region, values read from the
     design (background and text colours, font family, size and weight) next
     to the values sampled at the same place in the capture. A different
     **token** is a defect: another named colour (a beige surface where the
     design is white, grey header rows that render white), another font
     family or weight, or a size more than 2 px off. Anti-aliasing, a 1 px
     shift, spacing within 4 px, and text or numbers that differ because the
     test data differs are not defects.
   - A listed frame with **no capture** is a `fail`, not `unverified`, when
     `input.frame_captures` is present: the dev agent declares the state in the
     work order's frame checklist and the harness captures it, so the evidence
     is within its reach. Name the frame and say the capture is missing.
     Without `input.frame_captures` (a repository the harness cannot run),
     fall back to the screenshots, and a frame no screenshot shows is
     `unverified`.
   - What a frame shows is required even when the work order's prose doesn't
     repeat it: the frames are part of the work order. Data, dates and order
     numbers in the frame are examples, not requirements.
   **A shared frame is judged only for the part this work order owns.** When
   the work order has a `## Frame scope` section (also in `input.frame_scope`:
   per frame, `owns` and `others` with the ticket that owns each other region)
   or a `## Shared frames` note, other tickets build the rest of that frame in
   their own PRs, in parallel:
   - Compare only the owned regions and elements. A difference in a region
     another ticket owns (its rows, its toolbar, its header) is a **note**, not
     a defect: list it in the PR's `notes` with the owning ticket, and do not
     fail the PR or any criterion for it.
   - In `input.design_checks`, a colour mismatch marked `owned: false` (with
     its `owner`) is in a region another ticket owns, or the work order gives
     it to another ticket: a note, never a defect. Where a mismatch has no
     `owned` mark, decide from the images whether it falls in an owned region.
   - A whole-page criterion on a shared frame ("Then the page matches D1") is
     judged for the owned part only; mark it `pass` when the owned part
     matches and say in the note that the rest belongs to the other tickets.
     The whole-frame match is checked after delivery by the design
     conformance run, which reports gaps between tickets.
   **A wrong data state is one test-data defect.** When a capture shows a
   different data state than the frame draws (the plan still loading, an empty
   or sparse list, seeded states the frame shows missing: no late or stacked
   orders, no overdue rows), the UI can't be judged there. Report **all** such
   gaps in **one** defect with `category: "test_data"`, as an object:
   `{"category": "test_data", "summary": "...", "criteria": [every criterion
   it affects], "regions": [...], "fix": "the frame scenario or seed to fix
   and what data it needs"}`. Mark the affected criteria `fail` with a note
   pointing at that defect (the one defect covers every criterion it lists).
   Do not raise separate UI defects for those regions, in this round or later
   ones, unless the UI itself is still wrong once the data is right.
   **A person's scope amendment always wins.** A `## Scope amendment (by
   <person>, <date>)` section in the work order was added by a person while the
   work order was running. It amends the work order: where it conflicts with
   anything above it (a frame, a criterion, the frame scope, an earlier QA
   ruling), the amendment wins, and the latest amendment wins over earlier
   ones. Meeting it is not a defect; judge the criteria as amended.
   **Check the base branch before calling something missing.** The patch shows
   only what changed. When a criterion names something the patch does not add
   (a translation key, a config entry, a helper), call `read_pr_file` with the
   PR URL, the path and `ref: base`; if the base branch already has it, the
   criterion is met and is not a defect.
   **Unit tests ran.** For a change with behaviour, the host verification or CI
   must show the repository's unit tests running and passing. A unit test run
   that failed (including a coverage threshold the repository sets) is a defect
   citing the command and its output; a repository with a unit test runner whose
   tests did not run at all is a defect too. A repository with no unit test
   runner is noted, not failed.
   **API types come from the backend's spec.** On a PR to a web repository that calls a
   backend (it has `src/api/openapi.json`, or its `.ts-harness.yml` names a
   `contract.openapi` repository), request and response types must come from the schema
   generated from that spec (`src/api/schema.d.ts`), and requests must go through the client
   typed by it. Each of these is a defect, and the PR is `failed`: a file in the patch that
   declares API request or response types by hand (or edits `schema.d.ts` by hand); a call to
   a path, method or field that `src/api/openapi.json` does not have (read it with
   `read_pr_file`); a changed `openapi.json` without a regenerated `schema.d.ts`. A web app
   that passes its tests with hand-written types still breaks against the real backend.
   **A test rewritten to fit is a defect.** `input.test_changes` lists the test
   names and assertions the patch removed, added or renamed. An existing test
   that now expects the opposite (allowed becomes refused, shown becomes
   hidden) without the work order asking for it is a defect: the behaviour the
   old test covered is a requirement the PR broke. Quote both lines.
   **Unverifiable is not failed.** When a criterion cannot be judged from what
   you were given (a patch marked `patch_truncated`, or a measurement such as
   a coverage percentage that neither CI nor host verify reports), mark that
   criterion `unverified` with a note saying what is missing and do not list it
   as a defect. A revision cannot produce evidence the gate never receives, so
   failing on it only repeats the run. Fail a PR only for a defect you can show:
   a failed check, code that contradicts a criterion, or a required behaviour
   with no implementation or test in a patch you can read in full.
   **A self-contradicting work order is not a defect in the PR.** When two
   statements in the work order cannot both hold (for example "sort newest
   first" and "keep the existing sort order unchanged") and the code follows
   one of them, mark the other criterion `unverified` with a note quoting both
   statements. Failing the PR sends it back to the dev agent, code review then
   asks for the opposite, and the work order loops; a person settles it.
4. **Report every gap in one pass.** Judge every acceptance criterion before you
   decide, and list each one that is not met as its own defect. Do not stop at
   the first failure: each QA rejection costs a full revision, and a second
   pass that only now mentions a gap that was there all along wastes one.
   For a frame, go through every owned region of it (surfaces, cards and their
   content such as progress bars, panels, clusters, controls, type) in this
   pass, not one kind of difference per round.
   Every criterion marked `fail` has a matching entry in `defects` (a
   `test_data` defect covers the criteria it lists).
   **Stay consistent with earlier rounds.** `input.previous_qa_rounds` lists
   your earlier verdicts on this work order, oldest first: each round's
   defects, failed criteria and notes. First check each earlier defect again
   (fixed or not). Do not reverse an earlier ruling (a layout you accepted, a
   region you said was another ticket's, a difference you called a note)
   unless the work order changed since, through a `## Scope amendment` or
   `input.human_change_requests`. A difference that was visible in an earlier
   round and not reported then is still a defect if it is one, but say in
   its note that it was missed earlier.
   For each PR, evaluate acceptance criteria and record evidence:
   - `acceptance_criteria`: per-criterion `pass`, `fail` or `unverified` with a short note.
   - `tests`: tests exercised or the host verify summary reused as evidence.
   - `defects`: blocking issues found (empty when the PR passes).
   - `notes`: differences that do not block, such as those in a region another
     ticket owns, each naming that ticket (optional).
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
        "notes": ["D1 toolbar spacing differs; the toolbar belongs to PROJ-13"],
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
- When code review and QA send it back in turn (review, QA, review), the
  harness stops revising and asks a human with both verdicts.

## Not In Scope

- Merging PRs (humans merge; the orchestrator resumes `await_human_merge`).
- Re-running the full delivery/build pipeline.
- Editing code or opening new PRs.
