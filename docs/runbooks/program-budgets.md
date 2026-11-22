# Program LLM budgets

Each delivery program can have an LLM budget for its whole life. It is not
monthly. A program's spend is the sum of `llm_usage.cost_usd` for that program:
proxy calls, agent calls made directly, and the token usage coding jobs report
(`result.usage[].cost_usd`). Team budgets ([team-budgets.md](team-budgets.md))
still apply on top: the team's monthly limit holds all of the team's work, and
the program budget controls one program.

A delivery run now costs about 5-15 USD per ticket, because revisions,
complex first runs and the pre-push review use the stronger model. A program
with 20 tickets can use a few hundred dollars.

| Program spend | What happens |
| --- | --- |
| No budget (the default) | Nothing. Spend still shows on the program page. |
| Below `warn_at_percent` (80% by default) | Nothing. |
| At `warn_at_percent` | One message in the program's Slack thread and one in the ops channel. Work goes on. |
| At 100%, `program_action: downgrade` (the default) | One message in the thread and the ops channel saying the action taken. Every new coding run of the program uses the default coding model: revisions and work orders marked `Complexity: High` do not switch to the stronger model, and the pre-push review runs on the default model. When a run was downgraded, its ticket's thread is told once. |
| At 100%, `program_action: stop` | One message in the thread and the ops channel saying the action taken. New coding jobs of the program stay queued with `held_reason = 'program budget reached'`, and each held ticket's thread gets one message saying it is on hold and how to release it. Jobs already running finish. |
| After a raise | Held jobs start on the next claim (within seconds), downgraded runs go back to the stronger model on their next run, and the warning is sent again if spend climbs back past it. |

Only programs that are `running` or `blocked` get messages. Agent steps that
are not coding jobs (planning, review gates, chat) are never held or
downgraded by a program budget; the team budget covers them.

## Configure

In `ts-harness-runtime.yaml`:

```yaml
budgets:
  warn_at_percent: 80          # shared with team budgets
  program_default_usd: 300     # every program without its own amount; 0 = no budget
  program_action: downgrade    # or stop
  notify_channel: C0123OPS     # defaults to SLACK_OPS_CHANNEL
```

The `budget_alerts` system job (every 5 minutes, see
`config/runtime-cron-jobs.json`) copies these values into `budget_settings`
(migration 051) and then sends team alerts, program alerts and ticket notes.
After a config change, restart the runtime; the job applies it within
5 minutes.

## Set a program's budget

Open the program in the admin UI. The **LLM budget** box shows the spend so
far, this month's spend, the budget and where it comes from (this program or
the default), and what happens at 100%. **Set budget** (or **Change budget**)
asks for:

- an amount for the whole program; leave it empty to go back to
  `program_default_usd`,
- a reason.

Admins can set any amount. Managers of the team that owns the program's
project can set up to `budgets.manager_max_monthly`. Everyone else sees the
box read-only. Every change writes a `program_budget_set` row to
`session_audit_log`.

The same works over the API:

```
GET /admin/budgets/programs/{program_id}   -> {"team": {...}, "program": {...}}
PUT /admin/budgets/programs/{program_id}   {"amount_usd": 500, "reason": "two more epics"}
PUT /admin/budgets/programs/{program_id}   {"amount_usd": null, "reason": "back to default"}
```

`program` has `budget_usd` (0 means none), `source` (`program`, `default` or
`none`), `spent_usd`, `spent_month_usd`, `percent`, `status` (`none`, `ok`,
`warning`, `reached`), `action`, `held_jobs`, `set_by`, `can_set` and
`set_limit`.

## Check it

```sql
SELECT p.id, COALESCE(p.display_name, p.summary) AS name,
       program_budget(p.id) AS budget, program_spend(p.id) AS spent
  FROM delivery_programs p
 WHERE p.status IN ('running', 'blocked')
 ORDER BY spent DESC;

SELECT id, work_order_id, held_reason, budget_note, budget_noted_at
  FROM coding_jobs WHERE held_reason IS NOT NULL OR budget_note IS NOT NULL
 ORDER BY created_at DESC LIMIT 20;

SELECT * FROM program_budget_alerts ORDER BY sent_at DESC LIMIT 20;
```

The coding launcher's claim response has `held_by_program_budget`, the number
of queued jobs it skipped for a program budget.

## Spend attribution

Coding usage is recorded against the job's program; when the job has none (the
runner could not look the program up), it is recorded against the program of
the job's workflow or its parent workflow (`workflow_program_id`). Migration
051 fills in the program on older usage rows that have a workflow but no
program.

## Failure behaviour

- Memory API down when a coding job is built: the run keeps its normal models
  (no downgrade).
- Slack down: alerts and ticket notes are marked as sent before posting, so a
  failed post is not retried. The program page still shows the status.
- Proxy on: the worker reports no usage itself and the proxy records it with
  the program from the job token or the workflow, so it is counted once.

## Not the same as `policy.budget_warn_usd`

`policy.budget_warn_usd` and `policy.budget_hard_stop_usd` (System Settings →
Controls) do not limit spend. `PolicyEngine.evaluate_budget` adds the cost it
is given to a per-tenant counter, and every caller passes `cost_usd=0.0`, so
the counter never grows and neither threshold is reached. Only the release
gate test exercises them. Use team and program budgets to limit spend.
