# Marketing-Agent Heartbeat

The marketing-agent heartbeat runs every 30 minutes during active hours. It uses `lightContext` and `isolatedSession`. Surface only items requiring human attention within 1 hour.

## Tasks

- **Campaign deadlines (every 30m):** Check the campaign calendar in Google Drive for any content pieces or launch events due in the next 48 hours with no completed draft. Surface to orchestrator with deadline and asset link.
- **Overdue drafts (every 30m):** Scan for content items marked "In Draft" that have passed their due date. Surface each with the number of days overdue.

## What NOT to Do
- Do not publish or schedule any content during a heartbeat — surface to orchestrator only.
- Do not pull full analytics datasets; save that for the `weekly-content-digest` cron job.
- Reply `HEARTBEAT_OK` if no campaigns are due within 48h and no drafts are overdue.
