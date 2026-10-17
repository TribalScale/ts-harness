# Hardened ADR Template

Use this ADR format in every Container, Component, and Feature Blueprint.

## Architecture Decision Records

### ADR-NNN: [Decision Title]

**Context:** [Why this decision was needed. Cite at least one specific Refinery NFR or state "No applicable NFR — decision is purely architectural" and explain why.]

**Alternatives Considered:**

1. **[Alternative 1 Name]** — [One paragraph describing how this option would work.]
2. **[Alternative 2 Name]** — [One paragraph describing how this option would work.]
3. **[Optional Alternative 3]** — [One paragraph if needed.]

**Decision:** [What was chosen.]

**Rejection Rationale:**

- **[Alternative 1 Name]** rejected because: [Concrete reason with number, citation, or scenario.]
- **[Alternative 2 Name]** rejected because: [Concrete reason with number, citation, or scenario.]
- **[Alternative 3 Name]** rejected because: [Concrete reason, if applicable.]

**Trade-off Scorecard:**

| Criterion | Chosen Option | [Alternative 1 Name] | [Alternative 2 Name] |
|---|---|---|---|
| Latency / Performance | [quantitative or "N/A — not in NFR"] | [quantitative or N/A] | [quantitative or N/A] |
| Cost | [$ per month/request/user or N/A] | [$ or N/A] | [$ or N/A] |
| Complexity (ops + cognitive) | Low / Medium / High — [why] | Low / Medium / High — [why] | Low / Medium / High — [why] |
| Team skill match | Low / Medium / High — [why] | Low / Medium / High — [why] | Low / Medium / High — [why] |
| Reversibility | Low / Medium / High — [why] | Low / Medium / High — [why] | Low / Medium / High — [why] |
| Agent Implementability | See full table below | See full table below | See full table below |

**Agent Implementability — Chosen Option: [Low / Medium / High] (rolled-up score: X.XX / 3.00)**

| Sub-dimension | Weight | Score | Justification |
|---|---:|---|---|
| Training-data prevalence | 2x | Low / Medium / High (1/2/3) | [Concrete evidence.] |
| Local testability | 2x | Low / Medium / High (1/2/3) | [Concrete verification path.] |
| Convention strength | 1.5x | Low / Medium / High (1/2/3) | [Convention evidence.] |
| Error feedback quality | 1.5x | Low / Medium / High (1/2/3) | [Compiler/runtime/test feedback evidence.] |
| Change-blast-radius | 1x | Low / Medium / High (1/2/3) | [Typical file/change scope.] |

**Calculation:** (2x[score] + 2x[score] + 1.5x[score] + 1.5x[score] + 1x[score]) / 8 = [sum] / 8 = [result] -> **[Low / Medium / High]**

**Binding Constraint:** [Lowest-scoring weighted sub-dimension and why it could flip the roll-up.]

Repeat the full Agent Implementability table, calculation, and binding constraint for every alternative in the scorecard.

**Consequences and Downstream Impact:**

- [Blueprint, component, Work Order, NFR, or future decision constrained by this choice.]
- [Risk or limitation introduced by this choice.]
- [Follow-up decision required, if any.]

**Challenge Response (CH-NNN):** [Add only when responding to adversarial review.]
