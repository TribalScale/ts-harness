# Agent Runtime

TS Harness runs an in-house Python runtime at `runtime/ts_harness/`.

## Components

- **Gateway** — HTTP API, auth, health, session routing
- **Agent loop** — provider-agnostic tool-use loop with iteration budgets
- **Tool registry** — registry-first tools with policy pipeline and audit
- **Memory client** — tenant-scoped adapter over `memory-gateway.py`
- **Skill catalog** — governed skill assignment with compatibility checks
- **Scheduler** — cron and heartbeat isolation
- **Channels** — Slack, Discord, Telegram adapters
- **Policy and audit** — tenant boundaries, RBAC hooks, immutable audit export

## Dispatch Flow

1. `workflow-runner.py` claims a ready task from memory-api
2. `GatewayHttpAdapter` POSTs to `/workflow/dispatch`
3. Runtime resolves agent, skills, session, and policy context
4. `EnterpriseAgent` runs the tool loop and returns `succeeded`, `blocked`, or `failed`
5. Workflow runner records `runtime_task_id` and completes/blocks/fails the task

## MCP Transport

- **Production default:** stdio JSON-RPC subprocess transport (`mcp_client.py`).
- **SDK path (opt-in):** set `TS_HARNESS_MCP_SDK=true` to use the MCP Python
  SDK on a background daemon event loop (`mcp_sdk.MCPBackgroundLoop`).
- **HTTP JSON-RPC:** supported for tool discovery and calls against remote MCP
  servers; SSE streaming is experimental and not required for core workflows.

## Shadow Mode

Set `RUNTIME_FAKE_PROVIDER=true` to run the runtime with a deterministic fake
model provider for smoke tests without external LLM credentials.
