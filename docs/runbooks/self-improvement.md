# Self-improvement: the foundation

The harness is building a loop that improves its own delivery: a weekly failure-pattern digest
that proposes changes, offline replay evaluation, automatic canaries with rollback, and the
harness filing and fixing its own bugs. This runbook covers the foundation those parts build on:

1. **Run tagging**: every agent step and coding job records the versions it ran with.
2. **The target and the guard**: autonomy (tickets merged without a person stepping in) and
   problems after merge, on the Live → Metrics tab, groupable by any recorded version.
3. **The improvement budget**: a monthly model-cost cap for improvement work, apart from delivery.
4. **The off-limits list**: what the loop may never change on its own.
5. **The harness-level loop**: the harness files bugs against its own repository and opens draft
   self-fix PRs a person merges (section 5).

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

## 5. Harness-level loop: the harness files and fixes its own bugs

Code: `runtime/ts_harness/improvement/harness_loop.py` (collect, file, comment),
`runtime/ts_harness/improvement/self_fix.py` (self-fix PRs), `runtime/ts_harness/improvement/sanitize.py`
(privacy). Ledger: table `harness_self_issues` (migration `057_harness_self_issues.sql`) behind
`GET /harness-issues`, `POST /harness-issues/occurrences` and `PATCH /harness-issues/<id>` on the
memory API. System job: `harness_self_issues` (cron `harness-self-issues`, every 10 minutes).

The product owner agreed: the harness may file bug reports against itself and open PRs on its own
repository. It never merges or deploys, and never changes anything on the off-limits list.

### What triggers filing

| Source | When | One occurrence is |
| --- | --- | --- |
| Stuck-step arbiter | A step waiting for a person whose `_arbiter` record has category `harness_issue` (Inbox label "Harness issue") | One stop of that step (task id and the stop's `since`) |
| Ops alert: lost jobs | `TS_HARNESS_ALERT_LOST_JOBS` (default 2) or more coding jobs lost in the last hour | One lost job |
| Ops alert: LLM proxy | `LLM_PROXY_URL` is set and its `/health` does not answer | One hour of failure |

Other ops alerts (stuck decisions, non-converging work, spend, disk) are about client work or the
host, not harness code, and are not filed. The memory API being down is not filed either: the
ledger lives behind it. The job reads the arbiter's record and runs the two harness-facing alert
checks itself, so it works whether or not ops alerts post to Slack.

### Dedupe and what an issue holds

A failure class is its **fingerprint**: a hash of the source, the harness component (the deepest
harness file in the traceback, else `step <name>`) and the sanitised error signature with ids,
hex strings and numbers normalised. A new class files one issue (labels `harness-bug` and
`harness:<source>`; at most 3 new issues per run). A known class gets a comment with the new
count ("Seen again: N occurrence(s) in total (was M)"), at most once every 6 hours per issue and 5
comments per run. Before creating an issue the job looks for an issue on the harness repository
whose body carries the same `ts-harness-fingerprint` marker, so a lost ledger does not duplicate
issues. An issue a person closes stays closed; new occurrences are still counted and commented.

Each issue carries: what happened (the source), the component and its files, the kind (`code`,
`configuration`, `operations`, `unknown`), the signature, the arbiter's diagnosis, the harness
stack frames, how often (count, first and last seen) and internal references (workflow type,
step, workflow, task and coding job ids, timestamps) to look up in the console.

### Privacy: the sanitiser

The harness repository never receives client data. Every text goes through
`sanitize.sanitize_text` and `safe_field`, with the install's client identifiers: every project
repository (with its role), project key and name, team, tenant display name, the repo allowlist,
Jira filing projects and the GitHub org, plus the task's repository, ticket keys, work order and
ticket text. If those identifiers cannot be read, **nothing is filed that run**.

| Rule | Becomes |
| --- | --- |
| Fenced code; diff, quote, table and bullet lines; lines that look like source code; traceback source lines | `<code removed>` |
| A GitHub URL or slug of a known client repository, or its bare name | `<web repo>`, `<api repo>` (its role), `<client repo>` |
| Any other GitHub URL or `owner/repo` | `<repo>` |
| Any other URL, an e-mail address | `<url>`, `<email>` |
| Known ticket keys and work order ids (also inside branch names), any ticket of a known project key, anything shaped like `ABC-123` | `<ticket>` |
| Project keys and names, teams, tenants, the GitHub org, distinctive words of the work order or ticket (CamelCase, ALLCAPS, capitalised mid-sentence) | `<name>` |
| A path that is not a file of the harness itself (a `runtime/ts_harness/...` path that exists in the package, or a `scripts/`, `config/`, `skills/`, `database/`, `admin-ui/`, `docs/` path that exists in the checkout) | `<path>`, or `<repo>` for `owner/name` |
| Quoted text that is not one identifier (`'pr_url'` stays) or a harness path | `'<text>'` |
| A line sharing a run of 5 words with the work order or ticket text | `<text removed>` |

Afterwards `sanitize.leaks` checks the result; a field where anything client-looking survives is
replaced by "(withheld: may contain client data; see the internal references)". The issue title
and body get a last check (`assert_clean`) before they are sent. When in doubt, it is left out:
the internal ids are always there to look the rest up inside the harness. The tests in
`tests/runtime/test_harness_loop.py` feed known client-looking strings through the whole sweep and
check that none reaches GitHub, the ledger or the self-fix work order.

### Self-fix PRs

For each filed, open issue with no self-fix yet:

1. **Fixable?** Only `kind = code` with at least one harness file. Configuration and access
   problems (credentials, tokens, 401/403, "not configured") and operations problems are labelled
   `needs-human` with the reason, and a comment says so.
2. **Off limits?** Every file the fix is expected to touch (the failure's harness files) is
   checked with `is_off_limits`. Any hit: no fix, label `needs-human`, a comment naming the file
   and why.
3. **Limits**: at most **2** self-fix PRs open (status `started` or `pr_open`), at most **3**
   started in any 7 days, **one per issue, ever** (the ledger refuses a second start), and one
   started per run.
4. **Budget**: the improvement budget must cover 20 USD (`SELF_FIX_ESTIMATE_USD`); otherwise the
   fix waits quietly (its status stays `none`) until the next month.
5. **Deliverable?** See "What an admin must configure". Otherwise the fix waits and the Metrics
   tab says what is missing.
6. **Start**: a `deliver_work_order` run (no parent workflow) with work order `HSF-<issue number>`
   built from the sanitised issue, `repo_url` the harness repository and `base_branch`
   `TS_HARNESS_SELF_REPO_BRANCH`. The work order requires a regression test that fails before the
   fix, the full test suite and ruff to pass, and no off-limits change. The normal flow runs:
   coding, CI, code review, QA and the human merge wait.
7. **Follow**: when the run opens its PR, the job labels it `harness-self-fix`, makes it a draft,
   comments that it is never merged automatically and links it from the issue. A revision run
   marks the PR ready when it ends, so the job drafts it again whenever the head commit changes
   (a person marking the same head ready is left alone). If the PR's actual files include an
   off-limits path, the PR is labelled `needs-human` with a "do not merge" comment. Merged,
   closed and failed runs are recorded. The run's coding job cost is charged to the improvement
   budget (purpose `harness_self_fix`) as it accrues; the gate agents' calls are metered in
   `llm_usage` as usual.

Nothing here merges, approves or enables auto-merge. Self-fix statuses: `none`, `not_fixable`,
`needs_human`, `started`, `pr_open`, `merged`, `closed`, `failed`.

The loop's own files (`sanitize.py`, `harness_loop.py`, `self_fix.py`,
`memory_routes/harness_issues.py`, migration 057), its limits (`MAX_OPEN_FIXES`,
`MAX_FIXES_PER_WEEK`) and switches (`TS_HARNESS_SELF_*`, `program_defaults.harness_loop.*`) are on
the off-limits list, so a bug in the loop itself is labelled `needs-human`.

### Switches and defaults

| Setting | Default | Meaning |
| --- | --- | --- |
| `TS_HARNESS_SELF_REPO` | unset | `owner/name` of the harness's own repository. Unset (or invalid): filing **and** self-fixes are off |
| `TS_HARNESS_SELF_ISSUES` | `on` | Issue filing. Off also stops self-fixes |
| `TS_HARNESS_SELF_FIX` | `on` | Self-fix PRs, independently of filing |
| `TS_HARNESS_SELF_REPO_BRANCH` | `dev` | The branch self-fix PRs target |

The same keys can live in the runtime config as `program_defaults.harness_loop` (`repo`,
`file_issues`, `self_fix`, `branch`); an environment variable wins. The variables are in the
instance env keys and the container env file (`scripts/phases/lib/common.sh`): set them in the
install config, then re-render the runtime env and restart the runtime (see
[runtime-config.md](runtime-config.md)).

### What an admin must configure

1. Apply migration 057 (`scripts/ops/migrate-db.sh`; it is in the ORDER list) and re-run phase 15
   (or merge the cron jobs) so `harness-self-issues` is scheduled.
2. Set `TS_HARNESS_SELF_REPO=owner/name`, and `TS_HARNESS_SELF_REPO_BRANCH` if the development
   branch is not `dev`.
3. Give the harness write access to that repository: install the GitHub App on it with
   **issues**, **contents** and **pull requests** read and write, or set a `GITHUB_TOKEN` with
   those scopes. Without issues write, filing fails and the Metrics tab says so.
4. For self-fixes, make the repository deliverable: add it to the repo allowlist
   (`program_defaults.repo_picker.repos`) or link it to a project in System → Projects, and onboard
   it like any repository (a `.ts-harness.yml` with its test commands, CI that runs on PRs into the
   development branch, branch protection that requires a person's review).
5. Optional: create the labels `harness-bug`, `harness-self-fix` and `needs-human` with colours
   (GitHub creates missing labels on first use).

Until 3 and 4 are done, the Metrics tab ("Harness issues") lists what is missing and no self-fix
starts; filing failures are recorded on each issue row (`last_error`) and retried next run.

### Where to see it

Live → Metrics → "Harness issues": the switches, open and weekly self-fix counts against their
limits, setup problems from the last run, and each filed issue (marked "Harness issue", linking
the GitHub issue) with its occurrences, self-fix status and PR link. `GET /admin/metrics` returns
it as `harness_loop`. Self-fix spend shows in the improvement budget under `harness_self_fix`.

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
| Keep client data out of harness-bound text | `sanitize.safe_field(text, ClientContext(...))`, `sanitize.leaks(text, ctx)` |
| Harness issues and self-fixes | `harness_loop.loop_status(memory)`, `GET /harness-issues` |
