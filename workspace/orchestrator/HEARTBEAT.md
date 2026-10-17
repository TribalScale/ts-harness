# Orchestrator Heartbeat

The orchestrator heartbeat runs every 30 minutes during active hours. It uses `lightContext` (only this file is loaded) and an `isolatedSession`.

## Tasks

- **Inbox scan (every 30m):** Check for urgent items in the Slack inbox, unread DMs to the bot, and any escalation flags left by specialists. If any item needs immediate human attention, post a summary to the ops channel per the hard-stops standing order. Reply `HEARTBEAT_OK` if nothing requires action.
- **Escalation check (every 30m):** Check `memory_audit` for any unresolved escalation entries from the last 30 minutes. Surface them to the user if found.

## What NOT to Do
- Do not spawn specialists during a heartbeat turn unless a hard-stop condition is active.
- Do not write to Slack channels other than the ops channel during a heartbeat.
- Do not fetch data from GitHub, Jira, or AWS during a heartbeat — defer to cron jobs.
