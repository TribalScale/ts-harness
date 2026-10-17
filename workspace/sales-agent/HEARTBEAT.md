# Sales-Agent Heartbeat

The sales-agent heartbeat runs every 30 minutes during active hours. It uses `lightContext` and `isolatedSession`. Surface only items requiring human attention within 1 hour.

## Tasks

- **Stale deals (every 30m):** Scan memory for active deals with no contact in > 3 days. Surface each deal name, last contact date, and a one-line suggested next action to the orchestrator. Do NOT send any outreach directly.
- **Scheduled meetings (every 30m):** Check Google Calendar for prospect meetings in the next 4 hours that have no meeting prep brief attached. Surface to orchestrator so a prep brief can be generated.

## What NOT to Do
- Do not send any email or external message during a heartbeat — surface to orchestrator only.
- Do not access engineering systems or internal Slack channels.
- Reply `HEARTBEAT_OK` if no deals are stale and no meetings are unprepared within 4 hours.
