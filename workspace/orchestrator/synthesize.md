# Synthesize — Combine Results into a Unified Response

## When to use

Use this skill **after** receiving results from **one or more** specialists—whether from a single **route-task** delegation, a **fan-out**, or a **sequential chain**. The orchestrator is the **only** voice to the user; specialists never reply directly.

Inputs are specialist structured objects: `{ status, result, confidence, warnings, follow_up_needed }`.

---

## Steps

1. **Collect** all specialist results with **metadata**: `status`, `confidence`, `warnings`, `follow_up_needed`, and optional labels (which subtask each answered).

2. **Conflict check**  
   Determine whether any `result` content **contradicts** another (e.g., ops says "healthy" while qa reports "blocking failures" for the same release).  
   - If **contradiction**: document the disagreement explicitly; follow **conflict resolution protocol** below.  
   - If **consistent** or **orthogonal**: proceed to structure the narrative.

3. **Overall confidence**  
   - **Floor:** minimum specialist `confidence` across included results (the user should not trust the combined answer more than the weakest critical link).  
   - **Reported value:** use the **average** of specialist confidences as the headline composite **unless** a single domain is outcome-critical—in that case, cite the **minimum** as the governing confidence.  
   - Always respect any specialist with `confidence < 0.7` for escalation behavior (per **route-task**).

4. **Structure the response**
   - **Lead** with the direct **answer** or **action taken** (don’t bury the lede).  
   - **Group by topic**, not by specialist (e.g., "Release health" merges ops + qa angles).  
   - **Confidence indicators** where material (e.g., low-confidence estimates, incomplete data).  
   - **Warnings and caveats** toward the **end** (or immediately after the section they qualify—pick one style and stay consistent within a single message).  
   - **Follow-ups:** if any specialist set `follow_up_needed`, list **clear** next options or required human actions.

5. **Channel adaptation**  
   Apply **format adaptation rules** (below): Slack/chat vs email vs report-style output.

6. **Follow-up presentation**  
   Aggregate all `follow_up_needed` flags into a short **"Next steps"** or **"Decisions needed"** block.

7. **Remember**  
   Store an **episodic** memory: what was asked, what was delegated, what the **synthesized** outcome was, and key numbers/dates for future recall.

---

## Conflict resolution protocol

| Scenario | Action |
| -------- | ------ |
| Factual disagreement (metrics, state) | State both claims; cite source/specialist domain; **prefer** time-bounded, verifiable data; suggest **one** concrete verification step (query, dashboard, rerun). |
| Recommendation disagreement (e.g., migrate vs not) | Present **pros/cons** from each perspective; do **not** fake consensus; invite human decision when two specialists disagree. |
| One failed / timed out, another succeeded | Synthesize **partial** answer; label the gap; no pretense of completeness. |
| Warnings vs clean result | Do not suppress warnings; align narrative so warnings **explain** any limitation of the headline answer. |

---

## Format adaptation rules per channel

| Channel | Rules |
| ------- | ----- |
| **Slack / chat** | Short paragraphs or bullets; **headline first**; link out to details; avoid walls of text; @mentions only if policy allows; thread for deep detail if needed. |
| **Email** | Greeting + **summary in first 2–3 sentences**; sections with headings; full caveats and follow-ups; attach or link reports. |
| **Reports / docs** | Table of contents or top summary; **methodology** for analyst outputs ("Data as of"); appendices for raw specialist detail if required. |
| **Status / exec update** | KPIs first, risks second, asks last; confidence as a single line if composite. |

Default to **brief** when channel is unspecified and the user habitually uses chat-like interfaces.

---

## Response templates

### Template A — Single-specialist result

```markdown
**Summary:** <one sentence outcome>

**Details:** <grouped explanation from result>

**Confidence:** <value>; <one line if low or conditional>

**Warnings:** <bullets or "None">

**Next steps:** <only if follow_up_needed or obvious user action>
```

---

### Template B — Multi-specialist fan-out

```markdown
**Summary:** <unified answer across domains>

### <Topic 1>
<merged content>

### <Topic 2>
<merged content>

**Overall confidence:** <average>; **governing floor:** <min> <if material>

**Warnings:** <aggregated>

**Next steps:** <consolidated follow_up_needed>
```

---

### Template C — Error / partial failure

```markdown
**Summary:** <what succeeded + what did not>

**Available:** <partial results by topic>

**Blocked / missing:** <failed or timed-out areas; retry note if applicable>

**Warnings:** <include failure context as user-visible caveats>

**Next steps:** <how to unblock or who/when to retry>
```

---

### Template D — Escalation needed

```markdown
**Summary:** <why automation stopped or confidence is too low>

**What we tried:** <brief>

**Where it failed / disagrees:** <specifics>

**Options for you:** <A / B / human handoff>

**Warnings:** <trust, approval, or risk flags>
```

---

## Confidence aggregation (quick reference)

- **Critical path:** overall trust ≤ **min**(confidences) for go/no-go decisions.  
- **Informational merge:** report **average** and mention **lowest** if < 0.7.  
- Always surface **warnings** even when average confidence looks fine.

---

## Related skills

- **route-task** — single delegation and escalation triggers  
- **fan-out** — parallel collection feeding this skill  
- **remember** — persist episodic summary after synthesis  
