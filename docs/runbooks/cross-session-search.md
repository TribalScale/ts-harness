# Cross-session Search Runbook

Cross-session search finds prior user and assistant transcript snippets for the current tenant. Results are snippets plus `session_id`, not full transcript panes.

## Search From Chat

1. Open Admin Chat.
2. Use the `Search transcripts` box above the recent thread list.
3. Submit the query.
4. Select a result to open the existing thread by `session_id`.

An empty query returns no hits. Search results are tenant-scoped and are never injected into `skill_prompt` or system context.

## Search From Tools

Agents can use the `search_sessions` tool with a query and optional limit. The tool returns transcript hits containing `session_id`, `seq`, `role`, `snippet`, `created_at`, `agent_id`, and `source`.

Use `session_id` with existing session history or Admin Chat thread opening when you need the full conversation. Keep `recall_memory` for curated memory lookup; cross-session search is for unbounded transcript logs.
