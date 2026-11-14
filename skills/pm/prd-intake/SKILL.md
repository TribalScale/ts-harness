---
name: prd-intake
description: Ask the clarifying questions for a PRD request (prd_request intake), each with a recommended answer the requester can accept with "yes". Never drafts the PRD.
category: pm
allowed_agents: [pm-agent]
version: 1.0.0
---

# PRD intake

Use on `prd_request` `intake` only. Never draft, outline or review the PRD in this step. The
requester answers in one reply, then `prd-drafter` writes the PRD from the request and that reply.

## Inputs

- The request text is the requester's own words.
- `input.sources`, when present, is what the requester attached (files, Drive documents, image
  descriptions), one markdown section per source. Treat it as part of the request.

## Find facts yourself

Facts are your job; decisions are the requester's. Before writing a question:

- Read every source. Never ask for something a source already states.
- Recall prior PRDs and decisions for the area (`recall_memory` with tags `prd` and the component
  area). Never ask what a prior decision already settled; mention it in that question's
  recommended answer instead.
- A source marked _Could not be read_ or listed under _Not read_ is missing. Ask about it once,
  naming it, and recommend how to proceed without it.

## Choose the questions

Ask only what would change the PRD. Cover, in this order and only where still unknown:

1. the problem and who has it, and how we know;
2. the people who use it and what each may and may not do;
3. what is in scope and what is explicitly out;
4. stated specifics: limits, numbers, intervals, technologies, data;
5. how success is measured, with a baseline if one exists;
6. constraints and timing.

Ask 3 to 7 questions. Skip any whose answer depends on another question in this round; the
drafter will record it as an assumption instead.

## Write each question

Each question is one string with the recommended answer after it:

`<question>? Recommended: <a concrete answer, with the reason in a few words>.`

- Word the question so that "yes" accepts the recommendation.
- Recommend something specific ("Admins only, because the export includes salaries"), never
  "it depends" or a list of options without a pick.
- If you cannot recommend without guessing at the problem, persona or outcome, say so in the
  question and recommend nothing.

## Output

Return JSON with `summary`, `questions`, `confidence`, `warnings` and `follow_up_needed`. The
runtime numbers the questions and shows `summary` above them, so the summary is one or two
sentences that end with: "Reply "yes" to accept all the recommendations, or give the numbers you
want to change."

```json
{
  "summary": "Before I draft the PRD for the expense export, a few decisions. Reply \"yes\" to accept all the recommendations, or give the numbers you want to change.",
  "questions": [
    "Should only finance admins be able to run the export? Recommended: yes, admins only, because it includes salaries.",
    "Is a monthly CSV enough for the first release? Recommended: yes; scheduled delivery can follow."
  ],
  "confidence": "HIGH | MODERATE | LOW",
  "warnings": [],
  "follow_up_needed": true
}
```

Tool, memory or recall problems go in `warnings`, never in a question.
