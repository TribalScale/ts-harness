# Deployment profiles

TS Harness runs in one of two shapes. Pick the one that matches the team size;
switching later is a config change plus a redeploy.

| Profile | Shape | Good for |
|---|---|---|
| `single_host` (default) | One EC2 host runs every container, coding runs locally | Teams of 2 to 10 people |
| `scale_out` | Fargate coding workers, replicated runtime and memory API, private VPC | 10 or more people, many programs at once |

`scale_out` is AWS only. The installer rejects it for GCP.

## Choosing a profile

Set it in the install config:

```yaml
cloud_provider: aws
environment: prod
deployment_profile: scale_out
```

The installer passes it to Terraform as `deployment_profile`. Terraform writes
`DEPLOYMENT_PROFILE` into the SSM runtime config, bootstrap hydrates it, and the
runtime config template renders it into:

```yaml
deployment:
  profile: scale_out
  overrides: {}
```

On `scale_out`, phase 12 adds `docker/docker-compose.scale-out.yml` to the
compose files.

## What the profile controls

The profile only chooses defaults. Each setting resolves in this order:

1. its environment variable,
2. `deployment.overrides` in the runtime config,
3. the profile default.

| Setting | Environment variable | `single_host` | `scale_out` |
|---|---|---|---|
| `coding_launcher` | `CODING_LAUNCHER` | `local` | `ecs` |
| `coding_max_running` | `CODING_MAX_RUNNING` | 3 | 20 |
| `lane_caps` | `WORKFLOW_RUNNER_LANE_CAPS` | `fast=10,agent=3,coding=3` | `fast=20,agent=6,coding=20` |
| `github_reconcile_interval_seconds` | `GITHUB_RECONCILE_INTERVAL_SECONDS` | 60 | 900 |
| `runtime_replicas` | `AGENT_RUNTIME_REPLICAS` | 1 | 2 |

Example: a small team that wants five parallel coding jobs on one host keeps
`single_host` and sets `overrides: {coding_max_running: 5}`.

Code reads these with `ts_harness.config.deployment_profile.resolve_setting(name)`.

`lane_caps` is the number of tasks one workflow runner runs at a time in each
lane. Runner replicas add up, so two `scale_out` runners run up to 12 agent
tasks. `WORKFLOW_RUNNER_LANE_CAPS=off` restores the single-runner claim loop.

## What scale_out builds on AWS

`scale_out` needs `network_mode = vpc` (the installer sets it unless the
`scale_out:` block says otherwise; `default` is rejected). Terraform then adds:

- A dedicated VPC with three public and three private subnets and one NAT
  gateway. The host moves to a public subnet and RDS to the private subnets.
- ECR repository `<project>-coding-worker` (scan on push, keeps 30 images).
- ECS cluster `<project>-coding` with Container Insights, and task family
  `<project>-coding-worker` (2 vCPU, 4 GB, 50 GiB ephemeral storage, logs in
  `/<project>/coding-jobs`).
- Worker networking with no public IP and no ingress. Egress reaches only the
  VPC endpoints (ECR, Secrets Manager, Logs, SSM, S3), the Squid egress proxy
  at `egress-proxy.<project>.internal:3128`, and the internal memory API load
  balancer.
- An internal load balancer in front of the host's memory API that forwards
  `/health` and `/coding-jobs/*` only. It serves HTTPS when
  `memory_api_certificate_arn` is set. `docker-compose.scale-out.yml`
  publishes port 18792 on the host, and the host security group admits it
  from the load balancer alone.
- Secret `coding-job-secret`, which signs worker job tokens. Rotating it
  invalidates tokens of running jobs; those jobs are swept as lost and retried.

The egress allowlist is GitHub, npm, PyPI, the LLM provider APIs and
`models.dev`/`opencode.ai`, plus `egress_allowed_domains`. Project registries
add domains at runtime through the `egress-extra-domains` SSM parameter.

Build the worker image with the **Build coding worker (AWS)** workflow, then
run **Deploy (AWS)** with `coding_worker_image_tag` set to the commit SHA it
printed.

## Switching from single_host to scale_out

Moving an existing install into the dedicated VPC replaces the host, its
security group and the RDS subnet group. Snapshot RDS and the data volume
first and plan a maintenance window.

1. Set `deployment_profile: scale_out` in the install config.
2. Run the installer plan and review the new VPC, ECR and ECS resources.
3. Apply, then re-run bootstrap phases 11 and 12 so the runtime config and
   compose overlay pick up the new profile.
4. Confirm `GET /healthz` reports `deployment_profile: scale_out`.

Switching back is the reverse. In-flight coding jobs on Fargate finish or time
out; new jobs start locally.
