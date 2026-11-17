# Scale-out dashboard and alarms

The `<project>-scale-out` CloudWatch dashboard shows queue depth per lane,
coding jobs, waits at approval gates, GitHub App quota and LLM spend per team.
Three alarms send to the `<project>-alerts` SNS topic.

## Where the numbers come from

| Source | What it adds |
| --- | --- |
| Memory API `GET /metrics/scale-out?window_seconds=120` | One snapshot from Postgres: ready, held and running tasks per lane; queued and running coding jobs, outcomes and durations in the window; gates waiting and decided; the last GitHub quota per installation; LLM tokens and cost per team |
| `github-rate-limits` system job (every 5 minutes) | Calls GitHub's `GET /rate_limit` for each active App installation (this call does not use quota) and stores the result on `github_installations` (migration 042) |
| `publish-runtime-metrics.sh` (cron, every 2 minutes) | Sends the snapshot to CloudWatch through `scale_metrics_payload.py`, in the `TSHarness` namespace with no `InstanceId`, because the values come from the shared database |
| Runtime `GET /metrics` | The same snapshot as Prometheus gauges (`ts_harness_lane_*`, `ts_harness_coding_job*`, `ts_harness_gate_*`, `ts_harness_llm_*`, `ts_harness_github_installation_rate_limit_remaining`), refreshed at most every 30 seconds |
| Workflow runner metrics file | `runner_coding_jobs_<status>` counters and `runner_coding_job_seconds` for the jobs that runner consumed |

## Alarms

| Alarm | Fires when | First checks |
| --- | --- | --- |
| `coding-job-queue-age` | The oldest queued coding job has waited over 15 minutes (`coding_job_queue_alarm_seconds`) for two checks in a row | Coding launcher logs and lease (`service_leases`), the ECS Fargate vCPU quota ([coding workers runbook](coding-workers-ecs.md)), per-team quotas, and `LaneHeldTasks` for teams over budget |
| `coding-job-lost-rate` | Over 20% (`coding_job_lost_rate_threshold`) of the coding jobs that finished in 15 minutes were lost, with at least 3 finished | Worker heartbeats: the egress proxy, memory API reachability from workers through the internal load balancer, and task stop reasons in ECS |
| `github-rate-limit-low` | Any installation has under 10% (`github_rate_limit_alarm_percent`) of its REST quota left | `GitHubRateLimitRemainingPercent` by installation on the dashboard. Polling jobs (`poll-github-merges`) and review loops are the usual causes. The quota resets hourly |

All three treat missing data as fine: an install with no coding jobs or no
GitHub App stays quiet.

## Check it

```bash
curl -fsS -H "Authorization: Bearer $MEMORY_API_TOKEN" \
  "http://127.0.0.1:18792/metrics/scale-out?window_seconds=600" | python3 -m json.tool
```

```bash
aws cloudwatch list-metrics --namespace TSHarness --metric-name LaneReadyTasks
```

A team that never appears in `CodingJobsRunning` or `LlmCostUsd` had no
activity in the window. Spend without a team is reported as `none`.
