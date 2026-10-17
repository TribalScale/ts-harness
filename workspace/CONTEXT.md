# Organizational Context

This file provides {{AGENT_NAME}} with the organizational context it needs to operate effectively. Replace all `{{PLACEHOLDER}}` values with your organization's actual information.

---

## Organization

| Field             | Value                           |
| ----------------- | ------------------------------- |
| **Company Name**  | {{COMPANY_NAME}}                |
| **GitHub Org**    | {{GITHUB_ORG}}                  |
| **Primary TZ**    | {{TIMEZONE}}                    |
| **Jira Projects** | {{JIRA_PROJECTS}}               |

---

## Team Structure

| Team Name          | Lead              | Members                                        | Primary Repo                 | Jira Project | Slack Channel       |
| ------------------ | ----------------- | ---------------------------------------------- | ---------------------------- | ------------ | ------------------- |
| `{{TEAM_1_NAME}}`  | `{{TEAM_1_LEAD}}` | `{{TEAM_1_MEMBERS}}`                           | `{{TEAM_1_REPO}}`           | `{{TEAM_1_JIRA}}` | `{{TEAM_1_SLACK}}` |
| `{{TEAM_2_NAME}}`  | `{{TEAM_2_LEAD}}` | `{{TEAM_2_MEMBERS}}`                           | `{{TEAM_2_REPO}}`           | `{{TEAM_2_JIRA}}` | `{{TEAM_2_SLACK}}` |
| `{{TEAM_3_NAME}}`  | `{{TEAM_3_LEAD}}` | `{{TEAM_3_MEMBERS}}`                           | `{{TEAM_3_REPO}}`           | `{{TEAM_3_JIRA}}` | `{{TEAM_3_SLACK}}` |
| `{{TEAM_4_NAME}}`  | `{{TEAM_4_LEAD}}` | `{{TEAM_4_MEMBERS}}`                           | `{{TEAM_4_REPO}}`           | `{{TEAM_4_JIRA}}` | `{{TEAM_4_SLACK}}` |

> Add or remove rows as needed. Each team the agent interacts with should have an entry.

---

## Systems Map

| System Name              | Purpose                           | Tech Stack                  | Hosting              | Owner Team          |
| ------------------------ | --------------------------------- | --------------------------- | -------------------- | ------------------- |
| `{{SYSTEM_1_NAME}}`      | `{{SYSTEM_1_PURPOSE}}`            | `{{SYSTEM_1_STACK}}`        | `{{SYSTEM_1_HOST}}`  | `{{SYSTEM_1_OWNER}}`|
| `{{SYSTEM_2_NAME}}`      | `{{SYSTEM_2_PURPOSE}}`            | `{{SYSTEM_2_STACK}}`        | `{{SYSTEM_2_HOST}}`  | `{{SYSTEM_2_OWNER}}`|
| `{{SYSTEM_3_NAME}}`      | `{{SYSTEM_3_PURPOSE}}`            | `{{SYSTEM_3_STACK}}`        | `{{SYSTEM_3_HOST}}`  | `{{SYSTEM_3_OWNER}}`|
| `{{SYSTEM_4_NAME}}`      | `{{SYSTEM_4_PURPOSE}}`            | `{{SYSTEM_4_STACK}}`        | `{{SYSTEM_4_HOST}}`  | `{{SYSTEM_4_OWNER}}`|
| `{{SYSTEM_5_NAME}}`      | `{{SYSTEM_5_PURPOSE}}`            | `{{SYSTEM_5_STACK}}`        | `{{SYSTEM_5_HOST}}`  | `{{SYSTEM_5_OWNER}}`|

> Map out every system the agent may need to interact with or reference. Include internal tools, third-party services, and infrastructure components.

### System Dependencies

```
{{SYSTEM_1_NAME}} → {{SYSTEM_2_NAME}} (API)
{{SYSTEM_2_NAME}} → {{SYSTEM_3_NAME}} (database)
{{SYSTEM_1_NAME}} → {{SYSTEM_4_NAME}} (authentication)
{{SYSTEM_5_NAME}} → {{SYSTEM_2_NAME}} (webhooks)
```

> Document how systems connect. This helps the agent understand blast radius during incidents.

---

## Key Contacts

| Role                       | Name                    | Contact Method            | Escalation Scenarios                               |
| -------------------------- | ----------------------- | ------------------------- | -------------------------------------------------- |
| Engineering Lead           | `{{ENGINEERING_LEAD}}`  | `{{TEAM_CHANNEL}}`        | Architecture decisions, team escalations, priority conflicts |
| Platform / DevOps Lead     | `{{PLATFORM_ADMIN}}`    | `{{OPS_CHANNEL}}`         | Infrastructure incidents, deployment failures, access issues |
| Security Lead              | `{{SECURITY_LEAD}}`     | `{{SEC_LEAD_CONTACT}}`    | Security incidents, access reviews, vulnerability triage |
| QA Lead                    | `{{QA_LEAD}}`           | `{{QA_LEAD_CONTACT}}`     | Test strategy, release readiness, critical bug triage |
| On-Call (current)          | Check `{{OPS_CHANNEL}}` | `{{OPS_CHANNEL}}`         | Production incidents, after-hours alerts             |
| {{AGENT_NAME}} Admin       | `{{PLATFORM_ADMIN}}`    | `{{ADMIN_CONTACT}}`       | Agent configuration, scope changes, capability issues |

> Keep this table current. The agent uses it to know who to loop in for different scenarios.

---

## Environments

| Environment   | URL                          | Deployment Method                 | Access Level      | Notes                         |
| ------------- | ---------------------------- | --------------------------------- | ----------------- | ----------------------------- |
| Development   | `{{DEV_URL}}`                | Deploy on explicit request | Full (agent + devs) | Ephemeral data, safe to break |
| Staging       | `{{STAGING_URL}}`            | Deploy merged code on explicit request | Full (agent + devs) | Mirrors production config     |
| Production    | `{{PROD_URL}}`               | Manual deploy with approval       | Read-only (agent) | Agent cannot deploy directly  |
| Preview       | `{{PREVIEW_URL_PATTERN}}`    | Auto per PR (if configured)       | Full              | Per-PR preview environments   |

### Environment Variables & Config

- Environment-specific config lives in: `{{CONFIG_LOCATION}}`
- Secrets are managed via: `{{SECRETS_MANAGER}}`
- Feature flags are managed via: `{{FEATURE_FLAG_SYSTEM}}`

---

## Glossary

Define company-specific terms, acronyms, and jargon so the agent understands your language.

| Term / Acronym  | Definition                                                                |
| --------------- | ------------------------------------------------------------------------- |
| `{{TERM_1}}`    | `{{DEFINITION_1}}`                                                        |
| `{{TERM_2}}`    | `{{DEFINITION_2}}`                                                        |
| `{{TERM_3}}`    | `{{DEFINITION_3}}`                                                        |
| `{{TERM_4}}`    | `{{DEFINITION_4}}`                                                        |
| `{{TERM_5}}`    | `{{DEFINITION_5}}`                                                        |

> Add every term that would confuse someone on their first day. This is how the agent avoids misunderstanding domain-specific language.

---

## Runbooks Index

| Runbook Name                          | Description                                              | Location                        |
| ------------------------------------- | -------------------------------------------------------- | ------------------------------- |
| `{{RUNBOOK_1_NAME}}`                  | `{{RUNBOOK_1_DESC}}`                                     | `{{RUNBOOK_1_LOCATION}}`        |
| `{{RUNBOOK_2_NAME}}`                  | `{{RUNBOOK_2_DESC}}`                                     | `{{RUNBOOK_2_LOCATION}}`        |
| `{{RUNBOOK_3_NAME}}`                  | `{{RUNBOOK_3_DESC}}`                                     | `{{RUNBOOK_3_LOCATION}}`        |
| `{{RUNBOOK_4_NAME}}`                  | `{{RUNBOOK_4_DESC}}`                                     | `{{RUNBOOK_4_LOCATION}}`        |
| `{{RUNBOOK_5_NAME}}`                  | `{{RUNBOOK_5_DESC}}`                                     | `{{RUNBOOK_5_LOCATION}}`        |

> Link to your operational runbooks. The agent should reference these during incidents and routine operations rather than improvising.

---

## Updating This File

This file should be reviewed and updated:
- When teams are restructured
- When new systems are added or decommissioned
- When key contacts change
- When new environments are provisioned
- Quarterly at minimum, as part of the monthly heartbeat audit
