---
name: draft-rfp
description: Draft an RFP response markdown artifact for governed sales review.
type: workflow
category: sales
allowed_agents: [sales-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.1.0
---

# Draft RFP

Draft a durable RFP response from clarified intake answers.

## Shared sources

`input.sources`, when present, is what the requester attached: files, Google Drive documents and descriptions of images, as markdown with one section per source. Treat it as part of the RFP request, alongside the clarified answers. Answer the RFP's own questions and requirements in its order when it provides them. Name the source when you rely on it. Do not ask for anything it already states. A section marked _Could not be read_, or listed under _Not read_, is missing: ask about it once (for a Drive link, ask the requester to share it with the workspace account), then carry on without it. A section cut at the size limit is incomplete; say so when the missing part matters.

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
