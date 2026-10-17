# Sprint Report — Weekly Progress Summary

## When to Use

Use this skill when:
- The weekly heartbeat trigger fires (typically Monday morning)
- A user requests a sprint or weekly report
- The orchestrator asks for progress summary at sprint boundaries

Do **not** use this skill for:
- Daily standups (use `standup-summary.md`)
- Release notes for external audiences (use `release-notes.md`)
- Incident details (use `incident-summary.md`)

---

## Steps

### 1. Fetch Jira Sprint Data

Gather sprint-level metrics from Jira:

**Active sprint tickets:**

```
jira_search(jql="project = <PROJECT> AND sprint in openSprints() ORDER BY status DESC, priority DESC")
```

**Completed this sprint:**

```
jira_search(jql="project = <PROJECT> AND sprint in openSprints() AND status = Done ORDER BY updated DESC")
```

**All sprint tickets (for totals):**

```
jira_search(jql="project = <PROJECT> AND sprint in openSprints()")
```

From the results, calculate:
- **Total tickets in sprint**: count of all
- **Completed**: count with status = Done
- **In progress**: count with status = In Progress
- **To Do / Not Started**: count with status = To Do
- **Blocked**: count with blocked flag or status

If story points are available, sum them for velocity calculation.

### 2. Fetch Merged PRs This Week

```bash
gh pr list --state merged --search "merged:>=$(date -u -v-7d +%Y-%m-%d 2>/dev/null || date -u -d '7 days ago' +%Y-%m-%d)" --json number,title,author,mergedAt,additions,deletions
```

Summarize:
- Total PRs merged
- Total lines added/removed
- Contributors list

### 3. Calculate Metrics

| Metric                | Formula                                      |
|-----------------------|----------------------------------------------|
| Completion rate       | (Completed / Total) * 100                    |
| Points delivered      | Sum of story points on completed tickets     |
| Points planned        | Sum of story points on all sprint tickets    |
| Delivery ratio        | Points delivered / Points planned             |
| Velocity              | Points delivered (compare to last sprint avg) |
| PR throughput         | Number of PRs merged this week               |

### 4. Identify Risks

Flag items that indicate potential problems:

| Risk Signal                              | Severity | Action                           |
|------------------------------------------|----------|----------------------------------|
| Ticket with no update in 5+ days         | High     | Check in with assignee           |
| Blocked tickets with no resolution path  | High     | Escalate to remove blocker       |
| Scope added mid-sprint (new tickets)     | Medium   | Note scope creep, discuss impact |
| Completion rate < 60% past mid-sprint    | Medium   | Flag burn-down risk              |
| Key dependency unresolved                | High     | Escalate to dependency owner     |

**Stale ticket detection:**

```
jira_search(jql="project = <PROJECT> AND sprint in openSprints() AND status != Done AND updated < startOfDay(-5) ORDER BY updated ASC")
```

### 5. Recall Incident History

```
memory_recall: tags=[incident, <this-week-date-range>]
```

Include any incidents that occurred during the reporting period with their severity and resolution status.

### 6. Compile Sprint Report

Build the report using the template below. Sections:
- **Progress**: The numbers and what they mean
- **Risks**: What could go wrong and recommended actions
- **Highlights**: Wins, notable achievements, team kudos
- **Next Week**: What's coming up, any planned changes

### 7. Return to Orchestrator

Provide:
- **Formatted report**: Ready for posting
- **Health indicator**: On Track / At Risk / Off Track
- **Top risk**: Single most important risk to address
- **Action items**: Numbered list of recommended actions

---

## Sprint Report Template

```markdown
# Sprint Report — Week of <DATE>

## Sprint Health: On Track | At Risk | Off Track

## Progress

| Metric              | Value        | Status     |
|---------------------|--------------|------------|
| Tickets completed   | 8 / 14      | 57%        |
| Story points        | 21 / 34     | 62%        |
| PRs merged          | 12          |             |
| Lines changed       | +1,204 / -387 |           |
| Sprint days remaining| 4           |            |

### Completed This Week
- TFS-123: JWT token refresh endpoint — @alice
- TFS-118: Fix stale search results — @carol
- TFS-125: Database index optimization — @dave
- <...>

### In Progress
- TFS-130: Bulk document upload (80% complete) — @alice
- TFS-127: Email notification system (50% complete) — @bob

### Not Started
- TFS-140: API rate limiting
- TFS-138: Node.js base image update

## Risks

| Risk                                  | Severity | Recommended Action            |
|---------------------------------------|----------|-------------------------------|
| TFS-135 blocked for 5 days            | High     | Escalate to partner team      |
| Completion rate below 60% at midpoint | Medium   | Consider scope reduction      |

## Incidents

- No incidents this week (or list incidents with severity)

## Highlights

- Search performance improved 40% after index optimization (TFS-125)
- Zero P1/P2 incidents this week

## Next Week

- Sprint planning on Monday
- TFS-130 expected to merge Tuesday
- Planned staging deployment Wednesday

## Action Items

1. Escalate TFS-135 blocker with partner team
2. Review remaining sprint scope in standup
3. Schedule staging deployment for Wednesday
```

---

## Metric Calculation Methods

| Metric           | Source                          | Calculation                                  |
|------------------|---------------------------------|----------------------------------------------|
| Completion rate  | Jira sprint tickets             | `done_count / total_count * 100`             |
| Velocity         | Jira story points (Done)        | Sum of points on completed tickets           |
| PR throughput    | GitHub merged PRs               | Count of PRs merged in date range            |
| Code churn       | GitHub PR stats                 | Total additions + deletions                  |
| Stale tickets    | Jira updated field              | Tickets not updated in 5+ calendar days      |
| Blocker age      | Jira status change history      | Days since ticket entered Blocked status     |

---

## Risk Classification

| Level      | Definition                                              | Sprint Impact      |
|------------|---------------------------------------------------------|--------------------|
| **High**   | Sprint goal at risk, immediate action required          | Off Track          |
| **Medium** | Potential delay, proactive attention needed              | At Risk            |
| **Low**    | Minor concern, monitor but no immediate action          | On Track           |
