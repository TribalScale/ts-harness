# Production alerts (ops Slack channel)

The harness checks itself every five minutes and posts to the ops Slack channel
when something needs a person. Before this, problems were only found when someone
noticed them.

The check is the `check-alerts` system cron job (handler `check_alerts`, every 300 s,
defined in `scripts/phases/15-automation-cron.sh` and `config/runtime-cron-jobs.json`).
It runs in agent-runtime, on the replica that holds the scheduler lease. Deploy results
come from `scripts/ops/redeploy.sh` instead (see [Deploy results](#deploy-results)).

## What it checks

| Alert | Fires when | Key | Default |
| --- | --- | --- | --- |
| Waiting for a decision | A step has waited for a person (revise, accept, retry or stop) longer than the threshold. Time counts from when the step was parked for a decision, or from its last update. Steps of a paused ticket, queue or program are left out while it is paused. Example: "PROJ-7 has waited 5 h for a decision at `finalize_code_review` (the automatic revisions ran out)." | `stuck:<task id>` | 4 h |
| Lost coding jobs | At least this many coding jobs were marked lost in the last hour (the worker stopped heartbeating or overran). | `lost_jobs:last_hour` | 2 |
| Not converging | A running work order's coding step (`deliver_work_order` or `implement`) has run at least this many times, the first run included. Each revision re-runs the step. | `revisions:<workflow id>` | 5 runs |
| Hourly spend | LLM spend recorded in `llm_usage` over the last 60 minutes is above the threshold. | `spend:hour` | $20 |
| Daily spend | LLM spend since 00:00 UTC is above the threshold. | `spend:day` | $150 |
| Disk | The filesystem at `TS_HARNESS_ALERT_DISK_PATH` is fuller than the threshold. In the container `/` is Docker's overlay root, so this is the host disk that holds Docker images and build cache. | `disk:<path>` | 85% |
| Memory API down | The memory API does not answer `GET /health`. | `health:memory_api` | on |
| LLM proxy down | `LLM_PROXY_URL` is set and the proxy does not answer `GET /health` within 5 s. | `health:llm_proxy` | on |

All alerts from one run go in a single Slack message.

## Repeats and resolved lines

- A new alert is posted once.
- While it keeps firing it is not posted again until `TS_HARNESS_ALERT_REPEAT_HOURS`
  (default 6) have passed. Then it is posted again, prefixed "Still firing:".
- When the condition clears, one "Resolved: ..." line is posted. Set
  `TS_HARNESS_ALERT_RESOLVED=false` to turn these off.
- If a check itself fails (for example the memory API is down, so the stuck-step and
  spend checks cannot run), its earlier alerts are kept as they were. They are not
  reported as resolved.
- If the Slack post fails, the alerts are not marked as posted, so the next run tries again.

What was posted when is stored in `runtime_kv`, namespace `ops-alerts`, key `state`, so a
restart or a new leader replica does not post everything again. While the memory API is
unreachable the job keeps the state in memory instead, so a memory API outage is posted
once, not every five minutes.

To see the current state:

```bash
curl -s -H "Authorization: Bearer $MEMORY_API_TOKEN" http://127.0.0.1:18792/runtime-kv/ops-alerts/state | jq .
```

## Where it posts

1. The ops channel through the Slack bot, when `SLACK_BOT_TOKEN` is set and a channel is
   configured: `budgets.notify_channel` in `ts-harness-runtime.yaml`, else `SLACK_OPS_CHANNEL`
   (the `slack_ops_channel` install setting). This is the same channel the
   [team budget](team-budgets.md) alerts use.
2. Otherwise the Slack incoming webhook in `SLACK_OPS_WEBHOOK_URL`.
3. With neither, the job does nothing and returns `{"status": "skipped"}`.

Make sure the Slack app is a member of the ops channel; otherwise Slack answers
`not_in_channel` and the job logs `ops alert post failed`.

## Thresholds

Every setting has a default. Set a threshold to `0` to turn that check off.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TS_HARNESS_ALERT_REPEAT_HOURS` | `6` | Hours before a still-firing alert is posted again |
| `TS_HARNESS_ALERT_RESOLVED` | `true` | Post a line when an alert clears |
| `TS_HARNESS_ALERT_STUCK_HOURS` | `4` | Hours a step may wait for a decision |
| `TS_HARNESS_ALERT_LOST_JOBS` | `2` | Lost coding jobs in one hour |
| `TS_HARNESS_ALERT_REVISION_RUNS` | `5` | Coding step runs on one running work order |
| `TS_HARNESS_ALERT_HOURLY_USD` | `20` | LLM spend in the last hour, USD |
| `TS_HARNESS_ALERT_DAILY_USD` | `150` | LLM spend since 00:00 UTC, USD |
| `TS_HARNESS_ALERT_DISK_PERCENT` | `85` | Disk use, percent |
| `TS_HARNESS_ALERT_DISK_PATH` | `/` | Filesystem to measure |

These reach agent-runtime through its `.env` file (written by phases 12 and 14), from the
host's `instance-env.sh`. To change one on a host:

```bash
echo "export TS_HARNESS_ALERT_DAILY_USD=300" | sudo tee -a /vol/ts-harness-data/.setup/instance-env.sh
sudo ADMIN_USER=<admin-user> bash scripts/ops/redeploy.sh
```

Phase 10a keeps values added to `instance-env.sh` by hand when the runtime-config store
does not set them. You can also add the same keys to the runtime-config store payload
(see [runtime config](runtime-config.md)). There is no Terraform variable for them yet.

## The ops webhook

`SLACK_OPS_WEBHOOK_URL` is optional. It is read from the secret `<prefix>/slack-ops-webhook-url`
by phase 10 and written to `secrets.env` and the runtime `.env`. A missing secret is
treated as unset.

1. In Slack, create an incoming webhook for the ops channel (Slack app settings,
   Incoming Webhooks).
2. Store it:

   ```bash
   aws secretsmanager create-secret --name "$PREFIX/slack-ops-webhook-url" --secret-string 'https://hooks.slack.com/services/…'
   # or, if it exists:
   aws secretsmanager put-secret-value --secret-id "$PREFIX/slack-ops-webhook-url" --secret-string 'https://hooks.slack.com/services/…'
   ```

3. Redeploy (phase 10 picks it up). Treat the URL as a secret: anyone with it can post to the channel.

## Deploy results

`scripts/ops/redeploy.sh` calls `scripts/ops/notify-ops.sh`, which posts through the webhook:

- On failure: `[host] redeploy FAILED (exit 1) at phase 12-compose-up on dev 1a2b3c4; see the redeploy log on the host`.
  The step is the one that was running (drain, a phase, migrations, image build, container
  start, phase 15, coding launcher start, prune).
- On success, as the last line: `[host] redeploy done 1a2b3c4 (dev)`.

`notify-ops.sh` reads `SLACK_OPS_WEBHOOK_URL` from the environment or from `secrets.env`,
does nothing without it, never fails the deploy and never prints the URL. Deploy results
use only the webhook, not the Slack bot, so they still arrive when agent-runtime is down.

To test it on a host:

```bash
sudo bash scripts/ops/notify-ops.sh "test message from $(hostname -s)"
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No alerts at all | `docker logs <agent-runtime> 2>&1 \| grep -i "ops alert"`. A result of `skipped` means no channel and bot token and no webhook. Check `SLACK_OPS_CHANNEL` and `SLACK_BOT_TOKEN` in agent-runtime's environment. |
| `ops alert post failed: not_in_channel` | Invite the Slack app to the ops channel. |
| `ops alert check <name> failed` | That check could not run (usually the memory API). Its alerts stay as they were until it runs again. |
| An alert never repeats | It repeats only after `TS_HARNESS_ALERT_REPEAT_HOURS`. Clear its entry by writing `{"active": {}}` to the state key to post everything that is firing again. |
| No deploy messages | `SLACK_OPS_WEBHOOK_URL` is missing from `secrets.env`; store the secret and re-run phase 10. |

## Related

- [Team LLM budgets](team-budgets.md): monthly budget warnings to the same channel
- [Scale-out monitoring](scale-out-monitoring.md): the CloudWatch metrics and alarms
- [Redeploy](redeploy.md)
