# Coding workers on ECS Fargate

On `deployment_profile: scale_out`, each queued coding job runs as one Fargate
task instead of a container on the host. The flow is the same as with the
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

The launcher type follows the profile (`ecs` on `scale_out`). Set
`CODING_LAUNCHER=local` in the host `.env` to fall back to host containers
without changing the profile.

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

**Worker image.** Run the **Build coding worker (AWS)** workflow, then
**Deploy (AWS)** with `coding_worker_image_tag` set to the SHA it printed.
The task definition pins that tag; a new image needs a new deploy.

**Secrets.** Workers read `coding-backend-api-key`, `coding-backend-provider`,
`coding-backend-model` and `github-token` from Secrets Manager through the
task execution role. They never receive `memory-api-token` or
`gateway-token`.

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

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Jobs stay "OpenCode queued", launcher logs `coding_job_launch_deferred` | Fargate vCPU quota or regional capacity | Raise the quota or lower `coding_max_running` |
| `coding_job_launch_failed` with `AccessDenied` | Host role lacks `ecs:RunTask` or `iam:PassRole` for the worker roles | Re-apply Terraform; check `<project>-coding-launcher` policy |
| Task stops with `CannotPullContainerError` | Image tag not in ECR | Run the build workflow, redeploy with that tag |
| Task stops with `ResourceInitializationError: unable to pull secrets` | Secret missing or execution role scope | Populate the secret; re-apply Terraform |
| Job goes lost with "no heartbeat" right after start | Worker cannot reach the memory API load balancer | Check the host security group admits port 18792 from the load balancer and the target group is healthy (`/health`) |
| Clone or `npm install` fails with 403 from the proxy | Domain not on the egress allowlist | Add it to `egress_allowed_domains` or the project registries; the proxy restarts on change |
| Cancelled job keeps running for a minute | Launcher stops tasks on its next tick (about 5 s) once it sees the job cancelled | None; the task gets `StopTask` with reason "coding job cancelled or lost" |

## Security notes

- Workers have no public IP and no inbound rules. Outbound traffic reaches
  only the AWS VPC endpoints, the egress proxy and the memory API load
  balancer, which forwards `/health` and `/coding-jobs/*` and returns 404 for
  everything else.
- The job token is a container override, so anyone allowed to call
  `ecs:DescribeTasks` on the cluster can read it. It only reaches that job's
  spec, heartbeat and complete endpoints and expires after four hours.
  Rotating `coding-job-secret` invalidates every outstanding token.
- The task role can only write to its own log group.
