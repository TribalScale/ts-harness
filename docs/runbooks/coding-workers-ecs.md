# Coding workers on ECS Fargate

On `deployment_profile: scale_out`, or with `runtime.coding_workers_on_ecs` on
`single_host`, each queued coding job runs as one Fargate task instead of a
container on the host. The flow is the same as with the
local launcher (see [implement-work-orders-opencode.md](implement-work-orders-opencode.md#coding-jobs-opencode-outside-agent-runtime)).
Only where the worker runs changes:

1. The runner parks the deliver step and creates a coding job.
2. `coding-launcher` on the host claims it and calls `ecs:RunTask` with the
   job id and job token as container overrides.
3. The task pulls `<project>-coding-worker:<tag>` from ECR, fetches its spec
   from the memory API through the internal load balancer, clones through the
   egress proxy, runs OpenCode, and reports the result.
4. The memory API records the branch and PR refs, resumes the parked step, and
   the runner validates the result as usual.

## Choosing the launcher

Where jobs run is resolved in this order:

1. A host override written by `scripts/ops/coding-launcher.sh local|ecs`
   (removed with `clear`). Use it to switch quickly, without Terraform.
2. `runtime.coding_launcher` in the install config (`local` or `ecs`), which
   reaches the host through the runtime config.
3. The profile: `ecs` on `scale_out`, `local` otherwise.

`runtime.coding_max_running` sets how many jobs run at once on either launcher.
On ECS, keep it within the Fargate quota (see below). If you switch back to
`local` with a high value, the host's memory check holds extra jobs until they
fit.

```bash
sudo ADMIN_USER=<admin-user> bash scripts/ops/coding-launcher.sh status
sudo ADMIN_USER=<admin-user> bash scripts/ops/coding-launcher.sh local
```

The switch waits until no coding job is queued, starting or running. It checks
twice, 60 s apart; `FORCE=1` skips the wait. It then re-runs phase 12, which
recreates only `coding-launcher`. Afterwards, make the lasting change in the
install config, then `clear` the override.

## ECS workers on single_host (default VPC)

You can run coding jobs on Fargate without `scale_out` or a dedicated VPC:

```yaml
runtime:
  coding_workers_on_ecs: true   # builds the Fargate stack
  coding_launcher: ecs          # runs jobs on it (local keeps the stack, unused)
  coding_max_running: 5
```

With `network_mode: default`, Terraform builds the cluster, the task
definition, ECR, the worker security group and the internal memory API load
balancer in the account's default VPC. Workers use the default subnets, except
the zones in `coding_ecs_excluded_zone_ids` (default `use1-az3`, where Fargate
does not run).

How this differs from private subnets:

- Each worker gets a public IP, used only for outbound traffic. Its security
  group has no inbound rules.
- Workers reach AWS APIs, GitHub, package registries and model providers
  directly, on ports 80 and 443 only. There is no egress proxy or VPC endpoint,
  so there is no domain allowlist. Registry **Test** connects directly too.
  For an enforced allowlist, use `network_mode: vpc`.
- The memory API port is published on the host whenever the stack exists, so
  switching launchers never recreates `agent-runtime`.

Worker size comes from the `scale_out:` block even on `single_host`
(`coding_worker_cpu`, `coding_worker_memory`,
`coding_worker_ephemeral_storage_gib`; default 2 vCPU, 4 GB, 50 GiB). Without
the LLM proxy, workers get `coding-backend-api-key`, `anthropic-api-key` and,
with OpenAI embeddings, `openai-api-key`. These are the same keys local workers
get.

Turning it on:

1. Check the Fargate quota (below).
2. Set the three keys above, then run `install.sh plan` and apply it.
3. On the host, redeploy (`scripts/ops/redeploy.sh`). Phase 10a picks up the
   `CODING_ECS_*` values, phase 12 publishes the memory API port, and the
   redeploy pushes the worker image to ECR before starting the launcher.
4. Watch the first job (see "Turn it on" below, steps 3 to 5).
5. If it misbehaves, run `coding-launcher.sh local`. Jobs go back to host
   containers, and the stack stays in place for the next try.

## Before the first run

**Fargate vCPU quota.** Each worker uses 2 vCPU by default, and the egress
proxy runs two 0.25 vCPU tasks. New accounts often start at a low regional
quota. Check it:

```bash
aws service-quotas get-service-quota --service-code fargate \
  --quota-code L-3032A538 --query 'Quota.Value'
```

Keep `coding_max_running` at or below `(quota - 1) / coding_worker_cpu_vcpus`.
For 20 parallel jobs at 2 vCPU, request at least 41 vCPU:

```bash
aws service-quotas request-service-quota-increase --service-code fargate \
  --quota-code L-3032A538 --desired-value 48
```

When the quota is reached, `RunTask` throttles. The launcher backs off, puts
the job back in the queue without spending one of its three launch attempts,
and logs `coding_job_launch_deferred`. Jobs wait as "OpenCode queued" until a
running worker finishes.

**Worker image.** `scripts/ops/redeploy.sh` pushes the worker image it has
just built to `<project>-coding-worker` in ECR. It pushes it under
`coding_worker_image_tag` (default `latest`, which the task definition pulls)
and under the commit's short SHA. Each new task pulls the image at start, so
workers always run the same harness code as the host, with no Terraform apply.
After the first apply, push by hand once:
`sudo ADMIN_USER=<admin-user> bash scripts/ops/push-coding-worker-image.sh`.
To roll workers back, set `scale_out.coding_worker_image_tag` to an older SHA
and apply.

You can still use the **Build coding worker (AWS)** workflow if the repository
has GitHub environments `<env>-plan` with `AWS_DEPLOY_ROLE_ARN` and
`AWS_REGION`. Deploy its SHA as `coding_worker_image_tag`. The next host push
then also updates that tag.

**Secrets.** Workers read `coding-backend-api-key`, `coding-backend-provider`,
`coding-backend-model` and `github-token` from Secrets Manager through the
task execution role. They never receive `memory-api-token` or
`gateway-token`. With a [GitHub App](github-app.md), workers swap
`github-token` for an installation token for their repository, fetched from
the runtime through the internal load balancer with their job token.

## Turn it on

1. Apply the `scale_out` install (see [deployment-profiles.md](deployment-profiles.md#what-scale_out-builds-on-aws)).
2. Re-run bootstrap phases 10 to 12 so the host picks up `coding-job-secret`
   and the `CODING_ECS_*` values from the runtime config, publishes the memory
   API port, and starts `coding-launcher`.
3. Check the launcher picked ECS:
   `docker logs coding-launcher 2>&1 | grep -m1 coding_job_launched` after the
   first job, or look for `runner_ref` values starting with `arn:aws:ecs:` in
   `GET /coding-jobs?limit=5`.
4. Start one work order and watch the task:
   `aws ecs list-tasks --cluster <project>-coding --started-by coding-job-<id prefix>`.
5. Logs: CloudWatch group `/<project>/coding-jobs`, stream `job/coding-worker/<task id>`.

## Fair share between people and teams

The launcher claims the oldest queued job that keeps its requester, program
and team under their quotas. Set them in the runtime config:

```yaml
scheduling:
  coding:
    per_user: 2      # running jobs per requester
    per_program: 3   # running jobs per program
    per_team: 8      # running jobs per team (the program's project team)
    global: 20       # optional; replaces CODING_MAX_RUNNING
```

A value of 0 removes that limit. `CODING_QUOTA_PER_USER`,
`CODING_QUOTA_PER_PROGRAM` and `CODING_QUOTA_PER_TEAM` override the file
without a restart of the runtime (restart `coding-launcher`).

## Project sizing and package registries

**System → Projects** in the admin console. Admins create projects and set the
owning team; admins and that team's managers change the rest.

- **Worker CPU / memory** override the task size for that project's jobs. The
  pair must be a valid Fargate combination (for example 4 vCPU with 8–30 GiB);
  leave both empty for the task definition default. Local Docker launches use
  the same values for `--cpus` and `--memory`.
- **Package registries** (npm, PyPI, Maven, Docker). The token is stored in
  Secrets Manager as `<secrets prefix>/projects/<project id>/registries/<registry id>`
  and is never returned. **Test** sends one authenticated request through the
  egress proxy (`CODING_EGRESS_PROXY_URL`) and records the result.
- Saving or deleting a registry rewrites the
  `/<prefix>/egress-extra-domains` SSM parameter with every registry host and,
  when it changed, forces a new deployment of the `egress-proxy` service. The
  proxy takes about a minute to roll; jobs started during the roll may see a
  403 from the proxy once and retry.
- A worker fetches its registries with its job token right after it starts
  and writes `~/.npmrc`, `~/.config/pip/pip.conf`, `~/.m2/settings.xml` and
  `~/.docker/config.json` with mode 600. If that fails, the job finishes with
  "package registry setup failed" and no code runs.
- Registry credentials need `CLOUD_PROVIDER=aws`; on other providers saving a
  credential returns an error.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Jobs stay "OpenCode queued", launcher logs `coding_job_launch_deferred` | Fargate vCPU quota or regional capacity | Raise the quota or lower `coding_max_running` |
| One person's jobs stay queued while others start | That requester, program or team is at its `scheduling.coding` quota (the claim reports `deferred_by_quota`) | Expected; raise the quota if the work is urgent |
| `coding_job_launch_failed` with `AccessDenied` | Host role lacks `ecs:RunTask` or `iam:PassRole` for the worker roles | Re-apply Terraform; check `<project>-coding-launcher` policy |
| Task stops with `CannotPullContainerError` | Image tag not in ECR | Run `scripts/ops/push-coding-worker-image.sh` on the host (or the build workflow) |
| Task stops with `ResourceInitializationError` mentioning a timeout (default VPC) | The subnet does not give the task a route out (no public IP) | Check `CODING_ECS_ASSIGN_PUBLIC_IP=true` in `coding-launcher.sh status` and the default subnets' internet gateway route |
| Task stops with `ResourceInitializationError: unable to pull secrets` | Secret missing or execution role scope | Populate the secret; re-apply Terraform |
| Job goes lost with "no heartbeat" right after start | Worker cannot reach the memory API load balancer | Check the host security group admits port 18792 from the load balancer and the target group is healthy (`/health`) |
| Clone or `npm install` fails with 403 from the proxy | Domain not on the egress allowlist | Add it to `egress_allowed_domains` or the project registries; the proxy restarts on change |
| Job fails with "package registry setup failed" | Memory API cannot read the registry secret (role scope or deleted secret) | Check the host role has `secretsmanager:GetSecretValue` on `<prefix>/projects/*`; re-save the registry token |
| Registry **Test** shows "request failed: ConnectTimeout" | Host cannot reach the egress proxy | Check the proxy security group admits 3128 from the host security group |
| Cancelled job keeps running for a minute | Launcher stops tasks on its next tick (about 5 s) once it sees the job cancelled | None; the task gets `StopTask` with reason "coding job cancelled or lost" |

Redeploys wait for running ECS jobs, as well as host containers, before they
restart the memory API. Their heartbeats and results go through it.

## Security notes

- In the default VPC, workers have a public IP for outbound traffic, no
  inbound rules, and egress only on 80 and 443 and to the load balancer.
  What follows describes private subnets.
- Workers have no public IP and no inbound rules. Outbound traffic reaches
  only the AWS VPC endpoints, the egress proxy and the memory API load
  balancer, which forwards `/health` and `/coding-jobs/*` and returns 404 for
  everything else.
- The job token is a container override, so anyone allowed to call
  `ecs:DescribeTasks` on the cluster can read it. It only reaches that job's
  spec, heartbeat and complete endpoints and expires after four hours.
  Rotating `coding-job-secret` invalidates every outstanding token.
- The task role can only write to its own log group.
