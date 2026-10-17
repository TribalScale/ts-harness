# Heartbeat — Scheduled & Triggered Routines

The heartbeat defines what {{AGENT_NAME}} does proactively — without being asked. Each routine specifies what happens, who handles it, what format the output takes, and where it goes.

All times are in `{{TIMEZONE}}` unless otherwise noted.

---

## Daily — 09:00

### 1. Morning Standup Summary

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `comms-agent`                                                |
| **Sources** | Jira board (`{{JIRA_PROJECTS}}`), GitHub activity (`{{GITHUB_ORG}}`) |
| **Action**  | Generate a standup summary: what was completed yesterday, what's in progress today, any blockers |
| **Format**  | Slack message with sections: Done / In Progress / Blocked    |
| **Post to** | `{{STANDUP_CHANNEL}}`                                        |

**Logic:**
- Pull Jira tickets transitioned to "Done" in the last 24 hours
- Pull Jira tickets currently "In Progress"
- Pull PRs merged in the last 24 hours from `{{GITHUB_ORG}}`
- Flag any tickets marked as blocked or with blocker-type labels
- Tag relevant team members next to their items

### 2. CI/CD Pipeline Health Check

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `ops-agent`                                                  |
| **Sources** | GitHub Actions / CI system across active repos               |
| **Action**  | Check pipeline status for all active repositories. Flag any failures or degraded pipelines |
| **Format**  | Summary table: repo, branch, status, last run time, failure reason (if any) |
| **Post to** | `{{OPS_CHANNEL}}`                                            |

**Logic:**
- Query CI status for `main`/`master` branch of each active repo
- If any pipeline is red: include failure logs summary and tag the last committer
- If all green: post a brief "All pipelines healthy" confirmation
- Track pipeline flakiness over time (store in memory)

### 3. Overnight Alert Review

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `ops-agent`                                                  |
| **Sources** | Monitoring system ({{MONITORING_TOOL}}), alerting system      |
| **Action**  | Summarize any alerts that fired between 18:00 and 09:00      |
| **Format**  | Grouped by severity: Critical → Warning → Info. Include alert name, time, duration, auto-resolved status |
| **Post to** | `{{OPS_CHANNEL}}`                                            |

**Logic:**
- Pull all alerts from the overnight window
- Group by severity, then by service
- For auto-resolved alerts: note them but don't alarm
- For unresolved alerts: flag clearly and recommend next steps
- If no alerts: post "Quiet night — no alerts fired"

### 4. Stale PR Check

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `dev-agent`                                                  |
| **Sources** | GitHub API (`{{GITHUB_ORG}}`)                                |
| **Action**  | Find open PRs that have been open > 3 days without activity  |
| **Format**  | List: PR title, repo, author, days open, last activity, reviewer status |
| **Post to** | `{{DEV_CHANNEL}}`                                            |

**Logic:**
- Query all open PRs across `{{GITHUB_ORG}}`
- Filter to those with no commits, comments, or reviews in the last 3 days
- Tag the PR author with a gentle nudge
- If a PR has been stale > 7 days, escalate to the team lead

---

## Weekly — Monday 08:00

### 1. Sprint Progress Report

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `analyst-agent`                                              |
| **Sources** | Jira board (`{{JIRA_PROJECTS}}`)                                           |
| **Action**  | Generate sprint progress: velocity, burndown, scope changes, risks |
| **Format**  | Structured report with metrics table, burndown summary, and risk flags |
| **Post to** | `{{TEAM_CHANNEL}}`                                           |

**Metrics to include:**
- Story points completed vs. committed
- Number of tickets: Done / In Progress / To Do / Added mid-sprint
- Velocity trend (last 3 sprints)
- Scope change percentage
- At-risk tickets (in progress but behind estimate)

### 2. Weekly Merged PRs & Release Notes

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `dev-agent`                                                  |
| **Sources** | GitHub API (`{{GITHUB_ORG}}`)                                |
| **Action**  | Summarize all PRs merged in the past 7 days. Group by repo. Generate draft release notes |
| **Format**  | Markdown: grouped by repo, each PR with title, author, and one-line summary |
| **Post to** | `{{DEV_CHANNEL}}`                                            |

### 3. Stale Jira Ticket Alert

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `comms-agent`                                                |
| **Sources** | Jira (`{{JIRA_PROJECTS}}`)                                                 |
| **Action**  | Flag tickets with no updates in > 5 days that are not in "Done" or "Backlog" status |
| **Format**  | List: ticket key, title, assignee, days since last update, current status |
| **Post to** | `{{TEAM_CHANNEL}}`                                           |

**Logic:**
- Query In Progress, In Review, and Blocked tickets with no activity in 5+ days
- Tag the assignee
- If a ticket has been stale > 10 days, flag for team lead attention

### 4. AWS Cost Anomaly Check

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `analyst-agent`                                              |
| **Sources** | AWS Cost Explorer, billing dashboards                        |
| **Action**  | Compare this week's costs to the 4-week average. Flag any service with > 20% increase |
| **Format**  | Table: service, this week, 4-week avg, % change, possible cause |
| **Post to** | `{{OPS_CHANNEL}}`                                            |

**Logic:**
- Pull daily cost data for the past 7 days, grouped by service
- Compare against the trailing 4-week average
- Flag services with > 20% cost increase
- Where possible, correlate with infrastructure changes (new deployments, scaling events)
- If no anomalies: "AWS costs are within normal range this week"

---

## Monthly — 1st of the Month, 09:00

### 1. Development Velocity Report

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | `analyst-agent`                                              |
| **Sources** | Jira, GitHub                                                 |
| **Action**  | Generate a monthly velocity and productivity report          |
| **Format**  | Executive summary + detailed metrics. PDF or Markdown.       |
| **Post to** | `{{LEADERSHIP_CHANNEL}}` or email draft to `{{ENGINEERING_LEAD}}` |

**Metrics:**
- Monthly velocity (story points completed)
- PR throughput (opened, merged, avg time-to-merge)
- Code review turnaround time
- Bug rate (bugs opened vs. closed)
- Deployment frequency
- Trend comparison vs. prior 3 months

### 2. Memory Maintenance

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | Memory system (orchestrator-managed)                         |
| **Action**  | Review and clean up the memory system                        |
| **Format**  | Internal log — no external post                              |

**Tasks:**
- Expire memories past their `expires_at` date
- Flag low-confidence memories (< 0.5) for review
- Archive memories not accessed in > 90 days
- Generate a brief memory health report: total memories, by type, by scope, expired this month

### 3. Skills & Automation Audit

| Field       | Value                                                        |
| ----------- | ------------------------------------------------------------ |
| **Agent**   | Orchestrator                                                 |
| **Action**  | Review heartbeat routines and agent capabilities for improvement opportunities |
| **Format**  | Internal recommendation list                                 |
| **Post to** | `{{ADMIN_CHANNEL}}` or direct to `{{PLATFORM_ADMIN}}`       |

**Tasks:**
- Identify routines that haven't produced useful output in the past month
- Suggest new automations based on repeated manual requests (check episodic memory)
- Flag any specialist agents that have had repeated failures
- Recommend configuration or scope adjustments

---

## On-Demand Triggers

These activate when a user says the trigger phrase (or close variants) in any approved channel.

| Trigger Phrase      | Action                                                      | Agent           | Output Format                          |
| ------------------- | ----------------------------------------------------------- | --------------- | -------------------------------------- |
| `standup`           | Generate today's standup summary (same as daily routine)    | `comms-agent`   | Slack message: Done / In Progress / Blocked |
| `deploy status`     | Query current deployment state across all environments      | `ops-agent`     | Table: service, environment, version, status, last deployed |
| `incident`          | Start incident summary protocol: gather alerts, recent deploys, error rates, affected services | `ops-agent` + `analyst-agent` | Structured incident brief |
| `sprint report`     | Generate current sprint progress (same as weekly routine)   | `analyst-agent` | Sprint metrics + burndown summary      |
| `who's on call`     | Look up the current on-call schedule                        | `comms-agent`   | Name, contact, rotation end date       |
| `cost report`       | Generate current cost summary and trends                    | `analyst-agent` | Cost table with trends and anomalies   |
| `pr status`         | Show all open PRs with review status                        | `dev-agent`     | PR list with review state and age      |

### Trigger Matching

- Match on the trigger phrase appearing anywhere in a message directed at {{AGENT_NAME}}.
- Fuzzy match allowed (e.g., "what's the deploy status?" matches `deploy status`).
- If multiple triggers match, ask the user which they meant.
- If no trigger matches but the intent is clear, route to the appropriate agent anyway.
