# Self-improvement: the foundation

The harness is building a loop that improves its own delivery: a weekly failure-pattern digest
that proposes changes, offline replay evaluation, automatic canaries with rollback, and the
harness filing and fixing its own bugs. This runbook covers the foundation those parts build on:

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
