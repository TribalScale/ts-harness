# Comms-Agent Heartbeat

The comms-agent heartbeat runs every 30 minutes. It uses `lightContext` and `isolatedSession`. Surface only items requiring a human reply within 1 hour.

## Tasks

- **Unread mentions (every 30m):** Scan for unread Slack @mentions of the bot user. For each mention that is a question or request, surface it to the orchestrator with context. Reply `HEARTBEAT_OK` if nothing requires attention.
- **Unread DMs (every 30m):** Scan for unread direct messages. Apply the same filter — surface only items needing a reply within 1h.

## What NOT to Do
- Do not reply to mentions or DMs directly during a heartbeat — surface them to the orchestrator.
- Do not send mass notifications or use @channel during a heartbeat.
