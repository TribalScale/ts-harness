# LLM proxy

Every model call from agents and coding workers can go through one metering
proxy. It records tokens and cost per tenant, team, program, workflow, task and
user, applies per-model rate limits shared by all replicas, and is where team
budgets are enforced. Provider keys live only in the proxy; callers hold
short-lived proxy tokens.

## How calls flow

| Caller | Proxy URL | Credential |
| --- | --- | --- |
| agent-runtime | `http://127.0.0.1:18795/llm` | token minted per model call, scoped to the workflow task or chat |
| agent-runtime-2 (replicas) | `http://agent-runtime:18795/llm` | same |
| local coding worker | `http://127.0.0.1:18795/llm` (shares the runtime namespace) | token minted by the launcher per job |
| ECS coding worker | `http://<internal-lb>/llm` | token minted by the launcher per job |

Clients call `/llm/<provider>/v1/<path>`: `openai`, `anthropic`, `openrouter`,
or any other name, which falls through to `MODEL_BASE_URL`. The proxy checks the
token, refuses with 402 if the team is over budget (see
[team budgets](team-budgets.md)), refuses with 429 (and
`Retry-After`) if the model's bucket is empty, swaps in the real key, relays the
response (streamed or not) and writes one `llm_usage` row. For streamed Chat
Completions it adds `stream_options.include_usage` so the final event carries
token counts.

OpenCode gets an `OPENCODE_CONFIG` file in the job workspace (outside the
repository) that points the provider's `baseURL` at the proxy with
`apiKey: {env:LLM_PROXY_TOKEN}`. Workers behind the proxy do not receive
provider keys: the launcher drops them from the worker environment, and on ECS
the `coding-backend-api-key` secret is no longer attached to the task
definition.

## Without the proxy

Usage is still recorded when calls go straight to the provider, as long as a
session database is configured (`TS_HARNESS_SESSION_DB_URL`, or
`LLM_USAGE_DB_URL` to use another one):

- Agent calls: each model call writes one `llm_usage` row (actor is the
  requesting user, or the agent) and one `agent_token_usage` row (by agent).
- Coding backend: after each OpenCode run the backend exports the sessions
  that run created in the repo (`opencode session list`, `opencode export`)
  and writes one row per model with `agent = coding-backend`. Conflict
  refreshes (`mode: refresh`) are not counted.

Prices come from `llm_pricing` in the runtime config. For a coding-backend
model the table does not list, the cost OpenCode reports is used instead.
With the proxy on, only the proxy records, so nothing is counted twice.

## Turn it on

1. Make sure a session database is configured (`TS_HARNESS_SESSION_DB_URL`);
   usage and rate buckets live in Postgres (migration 040).
2. Terraform: set `llm_proxy_enabled = true`. This creates the
   `llm-proxy-secret` signing secret and, on `scale_out`, an internal load
   balancer rule that sends `/llm/*` to host port 18795. It also sets
   `LLM_PROXY_ENABLED` and `CODING_WORKER_LLM_PROXY_URL` in the runtime
   config.
3. Re-run phases 10 and 12 (or the full bootstrap). Phase 12 adds
   `docker-compose.llm-proxy.yml` and refuses to start without
   `LLM_PROXY_SECRET` or a session database.

Without Terraform, set `LLM_PROXY_ENABLED=true` and put `LLM_PROXY_SECRET`
(32+ random characters) in the secrets env before phase 12.

## Prices and rate limits

Both live in the runtime config (`ts-harness-runtime.yaml`):

```yaml
llm_pricing:            # USD per million tokens
  "anthropic/claude-opus-5-5": {input_per_mtok: 4, output_per_mtok: 20, cache_read_per_mtok: 0.2}
  "openai/gpt-6.1-sol":        {input_per_mtok: 2, output_per_mtok: 10, cache_read_per_mtok: 0.2}

llm_rate_limits:        # shared across proxy replicas
  "anthropic/*": {requests_per_minute: 120, burst: 20}
```

Keys match `provider/model`, then `model`, then the longest `prefix*`, then
`*`. Cache reads default to a tenth of the input price; cache writes are
priced as input. A model with no price records zero cost. The shipped
template lists every model it assigns; add a row when you assign a new one.
Restart `llm-proxy` (and agent-runtime, for direct calls) after changing
either table.

## Check it

```bash
docker compose ... exec llm-proxy python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18795/health').read())"
```

```sql
SELECT team_id, month, sum(calls) AS calls, sum(cost_usd) AS usd
  FROM llm_usage_monthly
 WHERE month = date_trunc('month', now())::date
 GROUP BY team_id, month
 ORDER BY usd DESC;
```

Rows with no `team_id` come from callers without a team: channel chats from
users outside any team, or tasks whose program has no team.

## Failure behaviour

- Database down: calls still go through; usage is not recorded (logged as
  `llm usage not recorded`) and rate limits are not applied.
- Proxy down: agent and coding calls fail. Turn the proxy off
  (`LLM_PROXY_ENABLED=false`, phase 12) to go back to direct provider calls.
- Token expired: agent tokens are minted per call. Coding job tokens last 8
  hours (`LLM_PROXY_JOB_TTL_SECONDS` on the launcher).
- Rotating `llm-proxy-secret` invalidates outstanding job tokens. Rotate
  between coding runs, or expect running jobs to fail and be retried.
