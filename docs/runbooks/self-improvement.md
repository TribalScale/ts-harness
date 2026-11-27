# Self-improvement

The harness is building a loop that improves its own delivery: a weekly failure-pattern digest
that proposes changes, offline replay evaluation, automatic canaries with rollback, and the
harness filing and fixing its own bugs. Sections 1 to 4 cover the foundation; sections 5 to 10
cover the digest, proposals, replay, canary and promotion. The foundation:

1. **Run tagging**: every agent step and coding job records the versions it ran with.
2. **The target and the guard**: autonomy (tickets merged without a person stepping in) and
   problems after merge, on the Live → Metrics tab, groupable by any recorded version.
3. **The improvement budget**: a monthly model-cost cap for improvement work, apart from delivery.
4. **The off-limits list**: what the loop may never change on its own.

Agreed goals:

| | Metric | Where |
| --- | --- | --- |
| Target | Share of finished tickets merged with no person stepping in | Metrics → "Merged without a person" |
| Secondary | Cost per ticket, time to merge | Metrics → "Cost per ticket", "Time to merge" |
| Guard (must not get worse) | Problems after merge | Metrics → "Problems after merge" |

## 1. Run tagging (`_versions`)

Code: `runtime/ts_harness/improvement/versions.py`.

At dispatch, the workflow runner (`scripts/workflow-runner.py`, `_stamp_versions`) builds a
`versions` dict for the step and stores it on the task input under the reserved key `_versions`
(one `PATCH /workflow/task/<id>/input`, skipped when unchanged). For a coding step the coding job
spec gets it too, as `versions`, with the job's models and pre-push review variant
(`coding_job_stage.with_versions`); the memory API returns it on the job record.

The agent never sees it: the runner leaves `_versions` out of the dispatch payload, and the agent
loop drops it again before building the prompt. Input maps copy only the keys they name, so it
never flows into another step's input. `revise_on_reject` copies a source task's input into a
revision; the revision is stamped again when it is dispatched.

Tagging never fails a dispatch: any error is logged (`run_tagging_error`) and the step runs.
`TS_HARNESS_RUN_TAGGING=off` turns it off.

Shape (any section may be missing):

```json
{
  "schema": 1,
  "workflow": {"type": "implement_jira_ticket", "version": "3.2.0"},
  "fragments": {"verified_merge": "1.10.0"},
  "step": "deliver_work_order",
  "skills": {"governed-code-review": "1.11.0"},
  "models": {"agent": "...", "coding": "...", "review": "...",
             "gates": {"review-agent": "...", "qa-agent": "..."}},
  "variants": {"pre_push": "gate_inputs", "canary": "b"}
}
```

- `skills`: the step's `skill_hints` at the catalog's live (approved, highest) version, plus the
  code review gate's skill when the pre-push review uses its rubric.
- `models.agent`: the step agent's model in the runtime config. In the runner process this is the
  rendered runtime config file; a console change to an agent's model shows once the runner reads
  the new config.
- `models.coding`, `models.review`: the coding job's model and pre-push reviewer.
- `models.gates`: the configured models of the gate agents (`review-agent`, `qa-agent`).
- `variants.pre_push`: the pre-push review variant (`legacy`, `gate_rubric`, `gate_inputs`).
- Other `variants`: ticket assignments (below).

A ticket's versions are its tasks' `_versions` merged oldest first (a later step's value wins).

### Assigning a ticket to a variant

```python
from ts_harness.improvement.versions import set_ticket_variant, ticket_variants

set_ticket_variant(workflow_id, "canary", "b")      # before the ticket's steps run
set_ticket_variant(workflow_id, "canary", None)     # clear
ticket_variants(workflow_id)                         # {"canary": "b"}
```

Assignments live in `runtime_kv` (namespace `improvement-variants`, key = workflow id). Set them
on the workflow that runs the coding step (for a Jira ticket, its `implement_jira_ticket` run).
Each step dispatched afterwards records them under `variants`. This PR only records variants;
acting on one (choosing a prompt or a model by variant) is the canary's job.

## 2. Metrics

`GET /admin/metrics` (same access as History), shown on Live → Metrics. Code:
`runtime/ts_harness/workflows/delivery_metrics.py` and `runtime/ts_harness/improvement/guard.py`.

New parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `group_by` | none | A dotted versions key (`variants.pre_push`, `skills.governed-code-review`, `models.coding`, `workflow.version`). The response gets `groups`: one full summary per value; tickets without a value are `(none)`. `facets.group_keys` lists the keys present in the period. |
| `guard` | `1` | `0` skips the GitHub, Jira and smoke lookups (their signals are then unknown). |
| `guard_days` | `14` | The post-merge window, 1 to 90 days. |

### Autonomy (the target)

A ticket is **autonomous** when it merged and no person stepped in before the merge. The merge
itself is a person's and does not count. `autonomy.rate` = autonomous / finished tickets (merged,
accepted, stopped, cancelled, failed; replaced and running tickets are left out), the same
denominator as first-try pass. `autonomy.merged_share` = autonomous / merged.

A person stepped in when any task of the ticket shows:

| Kind | Read from |
| --- | --- |
| `decision_retry`, `decision_revise`, `decision_accept`, `decision_stop` | A decision on a stuck step by anyone but the harness, from the step's `_decision_log` (written by `human_decisions.decide` from this release) |
| `jira_reply` | A person answered the ticket's readiness questions on Jira (actor `jira-reply`) |
| `revise_grant` | A human revise grant (`_revision_grant`, from `/revise` or the Inbox) |
| `pr_changes_requested`, `pr_closed` | A "Request changes" review or `/revise` comment on the PR, or the PR closed (the merge wait's error, or a revision sourced from it) |
| `human_change_requests` | A revision that carried a person's change requests |
| `accepted_by_person` | A person accepted a blocked step as it is (`_human_accept`) |
| `stopped_by_person` | A person stopped the ticket |

The harness's own actors (`human_decisions.HARNESS_ACTORS`: `harness-arbiter`, `harness-triage`,
`harness-drafter`, `harness-cancelled`, `workflow-runner`, `orchestrator`, and any `harness-*`)
never count. A shared gateway-token sign-in (`admin`, `system`) is a person.

**`jira-reply` counts as a person.** The ticket could not go on until someone answered on Jira,
which is the kind of wait the loop is meant to remove. Each kind is reported separately
(`autonomy.by_kind`), so it can be read apart. To be confirmed by the product owner.

Retry decisions made before this release were not logged; earlier tickets can look more
autonomous than they were (grants, accepts, stops and PR rejections were already recorded).

### Problems after merge (the guard)

Per merged ticket, within `guard_days` of the merge:

| Signal | Source |
| --- | --- |
| `reverts` | Commits on the PR's base branch in the window whose message reverts it: `This reverts commit <merge sha>`, `Reverts owner/repo#N` or `Revert "<PR title>"` (GitHub: one PR read and one commit list) |
| `jira_bugs` | Jira issues of type Bug created in the window that link to or mention the ticket key (one JQL search) |
| `smoke_failures` | New nightly smoke failures recorded after the merge for the PR's repository: as the web repository (its de-duplication record) or as the API a web repository's smoke ran against (an index the smoke handler now keeps, key `api:<owner>--<repo>` in namespace `nightly-smoke`) |
| `arbiter_overruled` | A person decided a step the stuck-step arbiter had already acted on (`after_arbiter` in the decision log). Counted for every finished ticket, not only merged ones, and shown with the guard but not added to "problems after merge" |

`problem_rate` = merged tickets with at least one revert, Jira bug or smoke failure / merged
tickets. A ticket whose window has not closed yet counts what is known so far (`window_open`).

Limits: at most 60 external lookups per request and 10 seconds, the most recent merges first.
Results are cached in the gateway process for an hour while the window is open and a day after it
closed. A source that cannot be read (no credentials, an error, over the limit) is `null` and
shows as **unknown**; it never fails the page. Smoke failures are those recorded as new after the
merge (the smoke record notes a failure again only after its earlier bug program closed and a day
passed) and only from 30 days back (the record's pruning).

### Reading metrics grouped by a variant

```
GET /admin/metrics?days=30&group_by=variants.canary
```

```python
from ts_harness.workflows.delivery_metrics import delivery_metrics

result = delivery_metrics(memory, days=30, group_by="variants.canary")
for group in result["groups"]:
    group["value"], group["summary"]["autonomy"]["rate"], group["summary"]["guard"]["problem_rate"]
```

## 3. The improvement budget

Code: `runtime/ts_harness/improvement/budget.py`; ledger: table `improvement_spend`
(migration `055_improvement_budget.sql`) behind `GET /budgets/improvement` and
`POST /budgets/improvement/charges` on the memory API.

- **Cap:** `budgets.improvement_monthly_usd` in the runtime config (default **150 USD**), or
  `TS_HARNESS_IMPROVEMENT_MONTHLY_USD`. 0 pauses improvement work.
- **Window:** the calendar month in UTC.
- **At the cap:** improvement work pauses until the next month. Delivery is never affected: its
  calls are not charged here and nothing here holds a delivery job. Improvement model calls are
  also metered into `llm_usage` with `agent=self-improvement` and no team or program, so they never
  count against a team or program budget.
- **Unreadable ledger:** `can_spend` answers no (fail closed for improvement work only).

```python
from ts_harness.improvement.budget import BudgetExhausted, ImprovementBudget

budget = ImprovementBudget(tenant_id="default")
budget.status()            # month, cap_usd, spent_usd, remaining_usd, paused, by_purpose, available
budget.remaining()
if budget.can_spend(0.50):
    with budget.track("weekly_digest", ref=digest_id, model=model) as call:
        text = call(prompt)                    # charged at its actual cost when the block ends
call = budget.model_call("proposal", model=model, ref=proposal_id)  # prompt -> text, like build_model_call
budget.charge(0.12, "replay", "replay-42")    # spend measured elsewhere
```

`track` and `model_call` raise `BudgetExhausted` before calling the model when the budget cannot
cover the estimate. Cost is the response's reported usage priced with `llm_pricing`, as the
metering proxy does. `can_spend` is advisory: two workers can both pass it, so the cap can be
exceeded by at most one call each.

Why a table and not `runtime_kv`: the ledger only grows, must be summed per month and by purpose,
and is written by several workers at once; rows with a month index do that without
compare-and-swap retries on one growing value, and keep each charge for audit.

The month's spend and the cap show on the Metrics tab ("Improvement budget").

To change the cap, set it in the runtime config and re-render it (see
[runtime-config.md](runtime-config.md)):

```yaml
budgets:
  improvement_monthly_usd: 150
```

Apply the migration with `scripts/ops/migrate-db.sh` (it is in the ORDER list).

## 4. What the loop may never change on its own

`runtime/ts_harness/improvement/off_limits.py` exports `OFF_LIMITS` and `is_off_limits(path=,
setting=, constant=, section=)`. Later PRs refuse (or send to a person) any proposal, patch or
canary that matches. The list covers:

- **Security rules:** the security scan, the skill guard and threat patterns, the policy engine and
  OPA policies, secret handling, `policy.*`, `sandbox.*`, the host security scan setting.
- **Gate blocking rules:** `code_review.py` (`is_blocking` and the verdict), the code review skill's
  Severity, Output Contract and Untrusted input sections, the QA gate skill's output contract and
  failure behaviour, stage contracts, merge policy and readiness, branch protection,
  `program_defaults.merge_readiness.*`, `program_defaults.governance.*`, verdict carry-forward.
- **Permissions and access:** gateway auth and access, program and knowledge access, `identity.*`.
- **Budgets:** team, program and improvement budgets (code, SQL, settings, the cap's env var), the
  metering proxy and usage recording.
- **The arbiter's limits:** `stuck_arbiter.py` (`MAX_ACTIONS_PER_STEP`, `MAX_PER_SWEEP`,
  `MIN_CONFIDENCE`, the override guards), `review_arbiter.py`, `TS_HARNESS_STUCK_ARBITER`.
- **The metrics and guard themselves:** `delivery_metrics.py`, `ticket_story.py`, `guard.py`,
  `versions.py`, `human_decisions.py` (what counts as a person) and the list itself.
- **The loop's own rules:** everything under `runtime/ts_harness/improvement/` (the replay pass
  rule, the canary stop rule, the screening), the proposal lifecycle (migration 056, its routes),
  `TS_HARNESS_CANARIES`, `TS_HARNESS_CANARY_SHARE`, `TS_HARNESS_IMPROVEMENT_*` and the
  `MIN_TICKETS`, `AUTONOMY_MARGIN` and `CATCH_MARGIN` constants.

A test checks that every listed path, Markdown section and constant exists, so a rename has to
update the list.

## Interfaces for later work

| Need | Use |
| --- | --- |
| What a step or job ran with | `task["input"]["_versions"]`, `coding_job["versions"]`; `versions.ticket_versions(tasks)` |
| One value | `versions.lookup(versions_dict, "variants.pre_push")` |
| Assign a ticket to a variant | `versions.set_ticket_variant(workflow_id, name, value)` |
| Compare variants | `GET /admin/metrics?group_by=variants.<name>` or `delivery_metrics(..., group_by=...)` |
| Who stepped in on a ticket | `guard.interventions(tasks)`, `guard.arbiter_overrules(tasks)` |
| Post-merge problems | `guard.post_merge(GuardContext(...), row, tasks, merged_at)` |
| Spend on improvement | `ImprovementBudget().can_spend / track / model_call / charge` |
| Is a change allowed | `off_limits.is_off_limits(path=..., setting=..., constant=...)` |

# The loop: digest, proposals, replay, canary

Built on the foundation above. Code: `runtime/ts_harness/improvement/` (`digest.py`,
`proposals.py`, `patches.py`, `replay.py`, `canary.py`, `loop.py`, `llm.py`); storage: migration
`056_improvement_proposals.sql` (tables `improvement_proposals` and `improvement_replay_cases`,
memory API routes under `/improvements/`).

```
weekly digest ──► proposal (proposed) ──► offline replay (evaluating)
                       │                      │ passed            │ failed ─► rejected
                       │ no offline check     ▼                   │ inconclusive ─► proposed (retry in 7 days)
                       ▼                  canary (automatic) ──► stop rule ──► rolled_back
                Inbox: a person               │ enough tickets, no worse
                approves / rejects            ▼
                                       awaiting_approval ──► Inbox: a person promotes ─► promoted (PR or by hand)
```

The loop may **start** a canary on its own once a proposal passes the offline replay. Full rollout
(promotion) always needs a person. Nothing on the off-limits list is ever proposed as an
actionable change or canaried. Every model call is charged to the improvement budget; when it is
used up the work pauses quietly until next month.

## 5. The weekly failure-pattern digest

System job `improvement-digest` (handler `improvement_digest`), checked daily, runs once every 7
days (the last run is kept in `runtime_kv`, namespace `improvement-loop`, key `digest`, so restarts
do not re-run it). `TS_HARNESS_IMPROVEMENT_DIGEST=off` turns it off.

It reads the tickets that finished in the last 7 days and collects, per ticket:

| Signal key | From |
| --- | --- |
| `review:<category>` | Blocking findings of the code review gate (`finalize_code_review`), by category |
| `qa:<category>` | QA gate defects |
| `repeated:<gate>` | The same finding came back in a later round (`revision_guidance.repeated_findings`) |
| `ci:<check>` | Failed CI checks (`failed_checks` of `await_pr_checks`), without a matrix suffix |
| `stuck:<reason>` | Why a step waited for a person (`_awaiting_human_decision.reason`) |
| `arbiter:<category>` | The stuck-step arbiter's classification (`_arbiter.category`) |
| `readiness:questions` | A hand-written ticket was not ready and its questions went to a person |
| `intervention:<kind>` | How a person stepped in (`guard.interventions`) |
| `post_merge:<signal>` | Reverts, Jira bugs, smoke failures after merge (merges of the last 21 days) |

**Threshold:** a signal is a pattern when at least **3 different tickets** show it in the week.
One or two tickets are as likely a quirk of those tickets as a cause in the harness; at a few
dozen tickets a week a higher bar would hide real recurring problems. Patterns are ranked by how
many tickets show them.

**Dedupe and cap:** a pattern already covered by an open proposal, or by one rejected or rolled
back in the last 28 days, is skipped; so is a proposal with the same title as an open one, and a
knowledge note that near-duplicates an active or proposed note. At most **5 proposals a week**
(counting earlier runs that week).

For each pattern one model call (`TS_HARNESS_IMPROVEMENT_MODEL`, else the revision diagnosis
model) asks for ONE proposal. The prompt carries the pattern, up to 8 examples, the off-limits
list, the open proposals' titles and, for review, QA and CI patterns, the text of the skill most
likely to need the change (so a diff applies exactly). Types:

| Type | What it carries | After the digest |
| --- | --- | --- |
| `knowledge_note` | A note (scope, kind, title, body, why, applies_to) | Created through the proposed-notes path (`source=drafted`, actor `harness-improver`); a person approves the note in the Inbox and the proposal follows it (promoted or rejected) |
| `skill_edit` | A unified diff to a file under `skills/` | Replayed when it only changes the review skill's reviewer sections (below); otherwise "no offline check available" |
| `setting_change` | Setting name, old value, new value | "No offline check available": a person decides |
| `fragment_change` | A unified diff to `config/fragments/*` or `config/workflows/*` (must stay valid JSON) | "No offline check available": a person decides |
| `needs_design` | The idea, why it is off limits, no diff | A person designs the change; it can only be dismissed |

Every proposal stores its evidence (pattern, ticket count, ticket and workflow references, PR
links, examples), the metric it should move, the expected effect, a risk note and its cost.

**Off limits:** `proposals.screen` checks every file a diff touches (and, for a Markdown skill,
every `##` section it changes) and every setting against `is_off_limits`. A hit turns the proposal
into `needs_design`: the diff is dropped, the idea and the reason are kept, and it is never acted
on. A diff that does not apply, touches a file outside its type's folders, or deletes a file is
dropped.

## 6. Offline replay (the pre-push reviewer)

Code: `replay.py`. The one target with an offline check is the pre-push reviewer's rubric: a
`skill_edit` to `skills/review/governed-code-review/SKILL.md` that changes only the sections the
pre-push reviewer reads and that are not off limits (Review depth, Deep checks, API types in a web
PR, Tests that change meaning, Review Checklist, Not In Scope, Reading beyond the diff; Severity
and Untrusted input are off limits). Target name `pre_push_review`.

**Cases** (`improvement_replay_cases`): for each delivered ticket of the last 60 days, its first
push as the code review gate judged it: the work order, the PR's changed files with patches and the
gate's scope check and test-change list (from the stored `review_precheck` output, or read from
GitHub with `compare` on the head the gate saw), and the gate's first verdict and blocking findings
as ground truth. Built on demand (at most 40 per run) when fewer than 5 blocks or 5 passes are
stored; kept 90 days.

**Evaluation:** the current rubric and the candidate (the skill with the diff applied) review the
same N cases (default **10**: up to 5 gate blocks and 5 gate passes, newest first), one model call
per case and version, with the patches inline (no checkout). Each version's verdict per case is
cached on the case (key: rubric hash and model), so re-evaluations and the current version's runs
are not paid twice. The budget is checked before every call.

| Score | Meaning |
| --- | --- |
| Catch rate | Gate-blocked cases the reviewer blocks |
| False-block rate | Gate-passed cases the reviewer blocks |
| Agreement | Cases where the reviewer's verdict matches the gate's |

**Pass rule:** the candidate's catch rate is at least **10 points** higher than the current one's
*and* it catches at least one more block, and its false-block rate is **no higher**. At least **3
blocks and 3 passes** must be replayed on both versions, otherwise the result is *inconclusive*
(also when the budget runs out mid-way). With 5 blocks, 10 points means at least one more caught
block; the false-block rule allows no extra false block, because a reviewer that blocks good work
costs a fix round on every ticket.

Results: passed → the canary starts (below); failed → `rejected`; inconclusive → back to
`proposed`, tried again after 7 days. Each evaluation is recorded on the proposal (`evaluation`,
history in `evaluations`) with its cost, which is added to the proposal's `cost_usd`.

Proposals that cannot be replayed yet (settings, fragments, other skills) are marked "no offline
check available" and wait in the Inbox for a person; they never start a canary.

## 7. The canary

Code: `canary.py`. System job `improvement-loop` (handler `improvement_loop`, hourly) runs the
replay for at most one due proposal, then the stop rule. `TS_HARNESS_IMPROVEMENT_LOOP=off` stops
the job; `TS_HARNESS_CANARIES=off` stops new canaries and new assignments.

**Start:** a proposal that passed moves to `canary` on its own, with `canary.started_at`, its
share and variant name `proposal-<id>`. **One live proposal per target** (`canary` or
`awaiting_approval`): a unique index enforces it; a proposal that passes while the slot is taken
goes back to `proposed` and is re-checked (cached verdicts make that cheap) when the slot frees.

**Assignment:** when a ticket's workflow starts (the first step the runner dispatches for an
`implement_jira_ticket` or `deliver_work_order` run created after the canary started), it is put in
`candidate` or `control` of variant `proposal-<id>` with `set_ticket_variant`: a stable hash of the
workflow and proposal ids against `TS_HARNESS_CANARY_SHARE` (default **0.5**, 0.05 to 0.95). Each
later step records it under `_versions.variants`. Tickets that started earlier are left out.

**How the variant changes behaviour:** when the runner builds a candidate ticket's coding job
(`coding_job_stage.with_canary`), the job's `review_rubric` (the pre-push reviewer's rubric, chosen
in `coding_job._review_settings`) is replaced by the candidate rubric: the review skill with the
proposal's diff applied, the reviewer's sections. Control tickets keep the current rubric. Once the
proposal is rolled back, candidate tickets get the current rubric on their next coding job. Any
error leaves the job as control; the canary never stops a job.

**Stop rule** (each hour, `delivery_metrics(..., group_by="variants.proposal-<id>", guard=True)`):

| Condition | Action |
| --- | --- |
| Any candidate ticket with a problem after merge (revert, Jira bug, smoke failure) | Roll back at once |
| The review skill changed so the diff no longer applies | Roll back |
| 8 candidate and 8 control tickets finished, candidate autonomy more than **12.5 points** (one ticket in eight) below control | Roll back |
| Same, candidate post-merge problem rate above control's | Roll back |
| Same, otherwise | `awaiting_approval`: Inbox, for a person to promote |
| Fewer than 8 per side after 45 days | Roll back (not enough tickets) |

A proposal waiting for approval stops getting new tickets but stays watched: a later problem on
one of its candidate tickets still rolls it back. The latest numbers (finished, autonomy, problem
rate, cost per ticket, per side) and the decision are stored on the proposal (`canary.latest`).

**Rollback** ends the experiment: new tickets are no longer assigned, candidate tickets review with
the current rubric from their next coding job, and the variant assignments stay for the metrics. A
person can also stop a live canary (Reject in the Inbox, or `POST /admin/improvements/<id>/decision`
with `rollback`).

## 8. Promotion

Proposals waiting for a person show in the Inbox as **Improvement** items (admins only): a canary
that passed (`awaiting_approval`), a proposal with no offline check, and `needs_design` ideas. The
detail shows the diff or setting, the evidence, the replay scores and the canary's
candidate-vs-control numbers. Approve and Reject call `POST /admin/improvements/<id>/decision`
(`approve`, `reject`, `rollback`; permission `edit_config`; audited as `improvement_<action>`).
A `needs_design` item can only be dismissed.

**What Approve does** (chosen: open a PR, never merge):

- `skill_edit` and `fragment_change`: when `TS_HARNESS_IMPROVEMENT_PR_REPO` (`owner/repo` of the
  harness) is set and the GitHub App or token can write to it, the harness opens a pull request
  against `TS_HARNESS_IMPROVEMENT_PR_BASE` (default `dev`) on branch `improvement/proposal-<id>-…`
  with the diff applied to that branch's files. It never merges it; review it like any change and
  raise the skill's `version` if needed. A skill edit to the review skill also changes the code
  review gate once merged (the gate reads the whole skill); the canary only changed the pre-push
  reviewer. Without the setting, or if the PR cannot be opened, the proposal is marked promoted
  "by hand" and the diff is shown for a person to apply.
- `setting_change`: recorded as promoted by hand; a person changes the setting (see
  [runtime-config.md](runtime-config.md)).
- `knowledge_note`: approve the note itself (a Notes item); the proposal follows.

## 9. Seeing it

Live → Metrics → **Improvements**: every proposal with its type, status, offline check result,
candidate vs control autonomy and ticket counts, cost, and a detail view; the month's improvement
spend and whether canaries are on. `GET /admin/improvements` returns the same.

## 10. Operating it

| Setting | Default | Meaning |
| --- | --- | --- |
| `TS_HARNESS_IMPROVEMENT_MODEL` | revision diagnosis model | Model for the digest and the replay (`off` stops both) |
| `TS_HARNESS_IMPROVEMENT_DIGEST` | `on` | The weekly digest |
| `TS_HARNESS_IMPROVEMENT_LOOP` | `on` | The hourly replay and stop-rule job |
| `TS_HARNESS_CANARIES` | `on` | New canaries and assignments |
| `TS_HARNESS_CANARY_SHARE` | `0.5` | Share of new tickets that get the candidate |
| `TS_HARNESS_IMPROVEMENT_PR_REPO` | unset | `owner/repo` for promotion PRs |
| `TS_HARNESS_IMPROVEMENT_PR_BASE` | `dev` | Base branch for promotion PRs |
| `TS_HARNESS_REPO_DIR` | the checkout | Where the harness's `skills/` and `config/` are read from |

Apply migration 056 with `scripts/ops/migrate-db.sh`. The two jobs are in
`config/runtime-cron-jobs.json` and phase 15 (re-run phase 15 to add them on a Postgres host).

To stop everything at once: `TS_HARNESS_CANARIES=off` (no new canary or assignment) and roll back
live canaries from the Inbox; or set the improvement budget to 0.
