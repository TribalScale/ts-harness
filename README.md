# TS Harness

TS Harness is a governed control plane for multi-agent work inside an
organization. People state intent in Slack, Discord, Telegram, the admin
console, or the HTTP API. The gateway classifies that intent, applies policy, and
either delegates a one-off task to a specialist agent or starts a durable
workflow with checkpoints, human approval gates, revise loops, and an audit trail.

New kinds of work are added as configuration: workflow definitions, routing
rules, agents, skills, and policy. The runtime does not need a fork per domain.
Product and engineering delivery is the first reference vertical; sales RFPs and
marketing content campaigns use the same path. See
[`docs/architecture/vision.md`](docs/architecture/vision.md).

## Quickstart (local, no cloud account)

Requires a current Docker Desktop (or Docker Engine with Compose v2) and Python 3.

```bash
scripts/quickstart.sh up
```

This builds and starts Postgres, the agent runtime, the memory API, the sandbox
worker, and the admin console. It uses a fake model provider, so no API keys
are needed. When the stack is healthy the script prints the console URL
(`http://localhost:8080`) and a gateway token; paste the token on the sign-in page.

- Use a real model: set `RUNTIME_FAKE_PROVIDER=false` and `OPENAI_API_KEY` or
  `ANTHROPIC_API_KEY` in `.quickstart/.env`, then run `scripts/quickstart.sh up` again.
  Memory recall also needs an embeddings key (`OPENAI_API_KEY`).
- `scripts/quickstart.sh gates` runs the CI release gates against the stack.
- `scripts/quickstart.sh down` stops it; add `--volumes` to delete its data.

## Lineage

TS Harness runs its own `agent-runtime` service. Several runtime modules adapt code from
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent); see
[`NOTICE`](NOTICE).

## Agent Topology

TS Harness preserves the orchestrator-plus-specialists model:

- `orchestrator` (default)
- `dev-agent`, `ops-agent`, `qa-agent`, `analyst-agent`, `pm-agent`
- `comms-agent`, `marketing-agent`, `sales-agent`

## Architecture

- `runtime/ts_harness/` — Python agent runtime (gateway, loop, tools, skills, policy)
- `scripts/` — workflow runner and memory gateway control plane
- `database/migrations/` — Postgres schema for memory and workflow state
- `workspace/` — rendered agent workspace templates
- `skills/` — governed enterprise skill catalog
- `docker/` — compose stacks for `agent-runtime`, `memory-api`, and `workflow-runner`

Client projects compose on top of this runtime. They do not fork it. See
[`docs/architecture/harness-blueprint.md`](docs/architecture/harness-blueprint.md).

## Runtime Contract

- `POST /workflow/dispatch` — workflow runner dispatch adapter
- `POST /session/message` — normalized channel messages
- `POST /cron/trigger` — scheduled agent runs
- `GET /healthz` — runtime readiness

## Install (AWS or GCP)

Select exactly one cloud at install time. Shared bootstrap and Compose run on
the VM; provider Terraform lives under `infra/aws` or `infra/gcp`.

**AWS**

```bash
cp config/install/aws.example.yaml /path/to/install.yaml
# Edit the YAML (account, region, state bucket, …), then:
./scripts/install.sh validate --cloud aws --config /path/to/install.yaml
./scripts/install.sh plan     --cloud aws --config /path/to/install.yaml
./scripts/install.sh apply    --cloud aws --config /path/to/install.yaml
```

**GCP**

```bash
cp config/install/gcp.example.yaml /path/to/install.yaml
# Edit the YAML (project, region, GCS state bucket, …), then:
./scripts/install.sh validate --cloud gcp --config /path/to/install.yaml
./scripts/install.sh plan     --cloud gcp --config /path/to/install.yaml
./scripts/install.sh apply    --cloud gcp --config /path/to/install.yaml
```

Prerequisites and post-apply steps: [`docs/runbooks/install.md`](docs/runbooks/install.md),
[`docs/runbooks/aws-install.md`](docs/runbooks/aws-install.md),
[`docs/runbooks/gcp-install.md`](docs/runbooks/gcp-install.md).

## Local Development

```bash
cp config/organization.example.yaml config/organization.yaml  # optional; edit for your org
pip install -e ".[dev]"
export GATEWAY_TOKEN=test-token
export RUNTIME_FAKE_PROVIDER=true
python -m pytest tests -q
ts-harness
```

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `AGENT_RUNTIME_URL` | Workflow runner dispatch target |
| `GATEWAY_TOKEN` | Bearer token for runtime auth |
| `MEMORY_API_TOKEN` | Bearer token for the memory API. Required unless `MEMORY_API_ALLOW_UNAUTHENTICATED=true` (local tests only). |
| `SANDBOX_WORKER_TOKEN` | Bearer token for the sandbox worker. Required unless `SANDBOX_WORKER_ALLOW_UNAUTHENTICATED=true` (local tests only). |
| `MEMORY_API_URL` | Memory gateway base URL |
| `TS_HARNESS_CONFIG` | Runtime config path |
| `SKILLS_DIR` | Skill catalog root |

## Docker

```bash
export ENTERPRISE_ASSETS_DIR="$(pwd)"
export GATEWAY_TOKEN=dev-token
export MEMORY_API_TOKEN=dev-memory-token
export SANDBOX_WORKER_TOKEN=dev-sandbox-token
docker compose -f docker/docker-compose.memory-api.yml -f docker/docker-compose.agent-runtime.yml -f docker/docker-compose.workflow-runner.yml up --build
```

## Contributing and security

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities as described in
[`SECURITY.md`](SECURITY.md), not in public issues. Changes are tracked in
[`CHANGELOG.md`](CHANGELOG.md); the release process is in
[`docs/releasing.md`](docs/releasing.md).

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Modules adapted from
hermes-agent keep their MIT notice; see [`NOTICE`](NOTICE).
