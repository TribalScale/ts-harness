---
name: prd-review
description: Review an approved or draft PRD before it becomes Jira work (jira_from_prd review_prd). Grades each dimension, audits requirement coverage, and returns the top 3 to 5 gaps. Read-only.
category: pm
allowed_agents: [pm-agent, orchestrator]
version: 1.0.0
---

# PRD review

Use on `jira_from_prd` `review_prd`, or when a PM asks "is this PRD ready?". Never write to Jira
or edit the PRD here; the review is advice for the story plan and the human gate.

## Input

`input.approved_prd`: the PRD markdown, or an object whose `prd_markdown` (or `result.prd_markdown`) holds it.
Read the `Profile:` header. A missing profile is itself a finding: infer it from the prd-drafter triggers.

## Grade each dimension

Rate each **Strong**, **Adequate**, **Weak** or **Missing**. A section reading `N/A: <reason>` is
complete; judge the reason. Never fault a Lite PRD for a Full-only section, but do flag a Lite PRD
that meets a Full trigger (more than one product, new entities, permission changes, billing,
audit or personal data, material load) as a critical gap naming the trigger.

1. **Problem and evidence**: who has it, how we know. A valid non-customer driver (platform need, migration, regulation) counts.
2. **Scope**: Non-Goals present; in and out are clear.
3. **Personas & permissions**: named personas; who can and cannot act.
4. **Requirement coverage** (the highest-value check):
   - every FR has `Specified in` and `Verified by`, and `Specified in` really describes the behaviour rather than naming it;
   - walk Proposed Solution, Key User Flows, states and settings for behaviour with **no FR**: those orphans become stories nobody planned;
   - FR and DoD ids are unique and none were renumbered.
5. **Failure paths**: every key flow has an `On failure` branch.
6. **Technical constraints**: every stated technology and limit is listed word for word.
7. **Non-functional** (Full): performance, accessibility, localization and observability, or `N/A` with who confirmed it.
8. **Definition of Done**: 5 to 10 verifiable, customer-observable items.
9. **Success metrics**: baseline and target for each; target-only is a gap.
10. **Open questions**: each has a named owner; note which would block a story.
11. **AI behaviour**: if the feature makes probabilistic decisions, a human approves or rejects them, and eval targets are named.

## Verdict

- `ready`: no critical gap. Stories can be planned now.
- `gaps`: at least one critical gap. Stories can still be planned, but the plan must carry each critical gap as an open question and the human decides at the plan gate.

A critical gap is one that would make a story unbuildable or untestable: an orphan behaviour, an FR
with no specification, a blocking open question, a mis-profiled PRD, or a missing failure path on a key flow.

## Output

```json
{
  "verdict": "ready | gaps",
  "critical_gaps": ["<what is missing, why it matters, the specific fix>"],
  "review_markdown": "# PRD Review: <title>\n## Snapshot\n## Strengths\n## Critical gaps\n## Notes for later\n## Hygiene"
}
```

In `review_markdown`: Snapshot is profile, length and FR count; 2 to 4 specific Strengths; the top
3 to 5 Critical gaps, numbered, each with a concrete fix ("FR-4 has no Verified by; add DoD-6 for
the export limit"), not "requirements are weak"; anything else under Notes for later. Quote the PRD
sparingly and cite sections by name.
