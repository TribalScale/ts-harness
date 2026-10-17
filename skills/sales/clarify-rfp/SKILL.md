---
name: clarify-rfp
description: Clarify RFP intake questions for a governed sales RFP workflow.
type: workflow
category: sales
allowed_agents: [sales-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.0.0
---

# Clarify RFP

Ask focused clarifying questions before drafting an RFP response.

## Output contract

Return JSON:

```json
{
  "questions": ["string", "..."],
  "summary": "optional short restatement of the request"
}
```

`questions` must be a non-empty array of concrete questions the requester can
answer in the bound Slack thread.
