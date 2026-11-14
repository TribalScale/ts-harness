---
name: clarify-rfp
description: Clarify RFP intake questions for a governed sales RFP workflow.
type: workflow
category: sales
allowed_agents: [sales-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.1.0
---

# Clarify RFP

Ask focused clarifying questions before drafting an RFP response.

## Shared sources

`input.sources`, when present, is what the requester attached: files, Google Drive documents and descriptions of images, as markdown with one section per source. Treat it as part of the RFP request; an attached RFP document usually answers scope, deadlines and evaluation criteria, so ask only about what it leaves open. Name the source when you rely on it. Do not ask for anything it already states. A section marked _Could not be read_, or listed under _Not read_, is missing: ask about it once (for a Drive link, ask the requester to share it with the workspace account), then carry on without it. A section cut at the size limit is incomplete; say so when the missing part matters.

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
