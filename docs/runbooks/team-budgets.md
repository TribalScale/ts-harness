# Team LLM budgets

Each team has a monthly LLM budget, measured from the cost the
[LLM proxy](llm-proxy.md) records. The default is 5,000 USD a month per team.

| Spend this month | What happens |
| --- | --- |
| Below the warning level (80% by default) | Nothing. |
| At the warning level | One Slack message to the ops channel. The team keeps working. |
| At 100% | One Slack message. New agent and coding work for that team waits in the queue with the reason "team budget reached". The proxy refuses the team's model calls with 402. Work already running finishes its current call and then fails on the next one; its retry waits in the queue. |
| After a raise | Waiting work starts on the next claim, and warnings are sent again if the team climbs back up. |

Fast-lane tasks (approvals, notifications, small hooks) are never held, so
humans can still approve, reject or cancel. Spend without a team (channel
chats from users outside any team, programs with no team) is shown separately
and never held.

## Configure

In `ts-harness-runtime.yaml`:

```yaml
budgets:
  currency: USD
  default_team_monthly: 5000   # every team without its own amount
  warn_at_percent: 80
  manager_max_monthly: 10000   # the most a team manager can raise to
  teams:                       # optional, by team slug or id
    data-platform: 8000
  notify_channel: C0123OPS     # defaults to SLACK_OPS_CHANNEL
```

The `budget-alerts` system job (every 5 minutes) copies these settings into
Postgres (migration 041), so a config change takes effect within 5 minutes of a
runtime restart.

## Raise a budget

Open **System → Budgets** in the admin UI. Managers see their own teams;
admins see every team plus unassigned spend. **Raise budget** asks for:

- the new monthly amount (managers are capped at `manager_max_monthly`;
  admins have no cap),
- whether it applies to this month only or from now on,
- a reason.

The order of precedence is: a raise for this month, then a permanent raise,
then `budgets.teams`, then `default_team_monthly`. The latest raise of each
kind wins. Every raise writes a `team_budget_raised` row to
`session_audit_log`.

Program pages show a banner when the program's team is near or at its budget,
with a link to the Budgets page.

## Check it

```sql
SELECT t.slug,
       team_monthly_budget('default', t.id, date_trunc('month', now())::date) AS budget,
       team_month_spend('default', t.id, date_trunc('month', now())::date) AS spent
  FROM teams t ORDER BY spent DESC;

SELECT id, step_name, team_id FROM workflow_tasks WHERE held_reason IS NOT NULL;
```

## Failure behaviour

- Database down: the proxy lets calls through (it caches each team's status for
  30 seconds), and claims stop anyway because they need the database.
- Proxy off: spend is not recorded, so budgets never fill and nothing is held.
