# Standup Summary — Daily Team Update

## When to Use

Use this skill when:
- The daily heartbeat trigger fires (typically morning)
- A user says "standup", "daily update", or "what's the status?"
- The orchestrator requests a team status summary

Do **not** use this skill for:
- Sprint-level reports (use `sprint-report.md`)
- Incident reports (use `incident-summary.md`)
- Individual ticket details (use `jira-to-pr.md`)

---

## Steps

### 1. Fetch Jira Board Status

Query Jira for current sprint ticket states:

**Tickets completed yesterday:**

```
jira_search(jql="project = <PROJECT> AND status changed to Done DURING (startOfDay(-1), startOfDay()) ORDER BY updated DESC")
```

**Tickets in progress:**

```
jira_search(jql="project = <PROJECT> AND status = 'In Progress' ORDER BY priority DESC")
```

**Blocked tickets:**

```
jira_search(jql="project = <PROJECT> AND (status = 'Blocked' OR labels = 'blocked' OR flagged = impediment) ORDER BY priority DESC")
```

**Tickets ready for review:**

```
jira_search(jql="project = <PROJECT> AND status = 'In Review' ORDER BY updated DESC")
```

Adjust the JQL project key and status names to match the board's actual configuration.

### 2. Fetch Recent Git Activity

Get commits and PRs from the last 24 hours:

**Recent PRs:**

```bash
gh pr list --state all --search "updated:>$(date -u -v-1d +%Y-%m-%d 2>/dev/null || date -u -d 'yesterday' +%Y-%m-%d)" --json number,title,state,author,updatedAt
```

**Recent commits to main:**

```bash
git log --since="24 hours ago" --oneline --format="%h %s (%an)" origin/main
```

### 3. Check for Active Incidents

```
memory_recall: tags=[incident, active]
```

Include any unresolved incidents or recent alerts in the standup.

### 4. Compile Standup

Organize items into the four standup categories. Apply the format rules strictly.

**Format rules:**
- Maximum 2 sentences per item
- Use ticket IDs as references
- Lead with the most impactful items
- Keep the total standup under 20 lines
- Use past tense for Done, present tense for In Progress, imperative for Blocked

### 5. Return to Orchestrator

Provide the formatted standup ready for posting.

---

## Standup Template

```markdown
# Daily Standup — <DATE>

## Done (Yesterday)
- Merged PR #142: JWT token refresh endpoint [PROJ-123] — @alice
- Closed PROJ-118: Fixed stale search results after contract edit — @carol

## In Progress
- PROJ-130: Implementing bulk document upload (PR draft open) — @alice
- PROJ-127: Database index optimization for clause search — @dave

## Blocked
- PROJ-135: Waiting on API credentials from partner team (3 days) — @bob

## Up Next
- PROJ-140: Email notification preferences (ready for pickup)
- PROJ-138: Update Node.js base image to 20.11 LTS

## Alerts
- No active incidents
```

---

## JQL Quick Reference

| Query                                         | Purpose                        |
|-----------------------------------------------|--------------------------------|
| `status changed to Done DURING (startOfDay(-1), startOfDay())` | Completed yesterday |
| `status = 'In Progress'`                      | Currently being worked on      |
| `status = 'Blocked' OR flagged = impediment`  | Blocked items                  |
| `status = 'In Review'`                        | Awaiting code review           |
| `status = 'To Do' AND sprint in openSprints()`| Upcoming in current sprint    |
| `priority in (Highest, High) AND status != Done` | High-priority open items    |

Adjust field names (`status`, `flagged`, `labels`) to match the project's Jira configuration.
