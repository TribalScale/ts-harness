# Skill: cost-awareness

Use this skill to track LLM token usage and prevent runaway API costs. Every specialist dispatch has a cost — make them intentional.

## When to use

- Before fanning out to 3+ specialists
- After each specialist dispatch (log usage)
- When users ask about system costs
- During daily standup and cost-anomaly-check cron jobs

## Logging token usage after each dispatch

After a specialist returns, log their token usage to the `agent_token_usage` table. Use session metadata if available from the specialist's structured output:

```sql
INSERT INTO agent_token_usage (agent_id, provider, model, input_tokens, output_tokens, session_id, recorded_at)
VALUES (
    'dev-agent',
    'anthropic',
    'claude-sonnet-4-5',
    1250,    -- input token count from specialist metadata
    480,     -- output token count
    '<session-id>',
    now()
);
```

If the specialist does not return token counts, omit the row rather than inserting zeros.

## Checking daily spend before large fan-outs

Before dispatching to 3+ specialists in parallel, check the rolling 24-hour token count:

```sql
SELECT
    SUM(input_tokens + output_tokens) AS total_tokens_24h,
    COUNT(DISTINCT agent_id)          AS active_agents
FROM agent_token_usage
WHERE recorded_at > now() - interval '24 hours';
```

**Thresholds:**

| Total tokens (24h) | Action |
|--------------------|--------|
| < 300,000 | Proceed normally |
| 300,000 – 500,000 | Note high usage in your response; still proceed |
| > 500,000 | Warn the user of high daily spend; ask for confirmation before large fan-outs |

These are soft thresholds — use judgment. An urgent incident warrants ignoring them.

## Viewing per-agent breakdown

```sql
SELECT agent_id, SUM(input_tokens + output_tokens) AS tokens_today
FROM agent_token_usage
WHERE recorded_at > now() - interval '24 hours'
GROUP BY agent_id
ORDER BY tokens_today DESC;
```

## Weekly cost summary (for cron / standup)

```sql
SELECT * FROM daily_token_summary
WHERE day >= now() - interval '7 days'
ORDER BY day DESC, total_output_tokens DESC;
```

## Model routing guidance

When cost is a concern and the task is simple or well-scoped:
- Prefer **sonnet-class** specialists over **opus-class** for drafting, summarizing, or retrieval tasks.
- Reserve **opus-class** for complex reasoning, architecture decisions, or tasks with high correctness requirements.
- Never downgrade a routing decision purely for cost if it would risk correctness or completeness for the user.

## Rules

- **Never** block a user's urgent task due to cost thresholds — warn and proceed.
- Log token usage **after** the dispatch returns, not before.
- If the specialist's structured output does not include token counts, note this and skip the log row.
- Token counts are estimates for awareness — they are not a billing source of truth (use AWS Cost Explorer or Anthropic Console for that).
