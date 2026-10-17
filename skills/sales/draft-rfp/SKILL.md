---
name: draft-rfp
description: Draft an RFP response markdown artifact for governed sales review.
type: workflow
category: sales
allowed_agents: [sales-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.0.0
---

# Draft RFP

Draft a durable RFP response from clarified intake answers.

## Output contract

Return JSON:

```json
{
  "rfp_markdown": "# RFP Response\n\n...",
  "summary": "optional one-line summary"
}
```

`rfp_markdown` must be non-empty markdown suitable for channel presentation and
human approval. Prefer presenting the draft in the bound channel
(`present_prompt: approval`) rather than writing files.
