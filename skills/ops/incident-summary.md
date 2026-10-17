# Incident Summary — Triage and Document

## When to Use

Use this skill when:
- The orchestrator reports an incident or system alert
- Monitoring detects anomalous behavior (container crash, high error rate, disk full)
- A user reports "something is broken" or "the system is down"
- A health check returns red status

Do **not** use this skill for:
- Routine health checks (use `health-check.md`)
- Planned maintenance or deployments (use `deploy-staging.md`)

---

## Steps

### 1. Recall Past Incidents

Query memory for incidents with similar symptoms:

```
memory_recall: tags=[incident, <symptom-keywords>]
```

Look for:
- Similar past incidents and their root causes
- Known failure modes for the affected components
- Previous mitigation steps that worked

### 2. Gather Current State

Collect diagnostic data from all relevant sources:

**Container logs (last 100 lines per service):**

```bash
docker compose logs --tail 100 --timestamps 2>&1
```

**Container status:**

```bash
docker compose ps
```

**System metrics:**

```bash
free -h
df -h /vol/ts-harness-data
uptime
```

**Recent Docker events:**

```bash
docker events --since "1h" --until "now" --format "{{.Time}} {{.Type}} {{.Action}} {{.Actor.Attributes.name}}" 2>/dev/null | tail -20
```

**Application error logs (if accessible):**

```bash
docker compose logs --tail 50 <app-container> 2>&1 | grep -i "error\|exception\|fatal\|panic"
```

### 3. Identify Timeline

Establish the incident chronology:
- **When did it start?** — Look at first error in logs, Docker events, or alert timestamp
- **What changed?** — Recent deployments, config changes, external dependency changes
- **Is it ongoing?** — Current container status, can the error be reproduced right now?
- **What is the blast radius?** — Which services are affected, which are healthy?

### 4. Classify Severity

| Severity | Definition                                                  | Response Time | Example                         |
|----------|-------------------------------------------------------------|---------------|---------------------------------|
| **P1**   | System down, all users affected, data at risk               | Immediate     | Database unreachable, app crash loop |
| **P2**   | Major feature broken, significant user impact               | < 1 hour      | Auth failing, API 500 errors    |
| **P3**   | Minor feature degraded, workaround available                | < 4 hours     | Slow queries, non-critical UI bug |
| **P4**   | Cosmetic or low-impact issue, no user-facing degradation    | Next business day | Log noise, minor config drift |

### 5. Identify Affected Services

Map the impact across the system:

| Service          | Status              | Impact                    |
|------------------|---------------------|---------------------------|
| Agent runtime    | Running / Down / Degraded | <description>       |
| Postgres         | Running / Down / Degraded | <description>       |
| Redis            | Running / Down / Degraded | <description>       |
| Tailscale        | Connected / Disconnected  | <description>       |
| External APIs    | Reachable / Unreachable   | <description>       |

### 6. Determine Mitigation Steps

Based on severity and root cause analysis, recommend immediate actions:

**Common mitigations:**

| Symptom                        | Immediate Action                              |
|--------------------------------|-----------------------------------------------|
| Container crash-looping        | Check logs, restart with `docker compose restart <svc>` |
| Disk full                      | Clear old logs: `docker system prune -f`; expand volume |
| Database connection refused    | Check Postgres container, restart if needed    |
| Out of memory                  | Identify memory-heavy container, restart       |
| Network/Tailscale down         | Check `tailscale status`, re-authenticate if needed |
| Application 500 errors         | Check app logs, recent deployment, rollback if needed |

For P1/P2 incidents, prioritize restoring service over root cause analysis.

### 7. Compile Incident Report

Use the template below to create the structured report.

### 8. Return to Orchestrator

Provide:
- **Severity**: P1-P4
- **Status**: active / mitigated / resolved
- **One-line summary**: What happened in plain language
- **Immediate action needed**: Yes/No — if yes, what specifically
- **Full incident report**: Attached below
- **Escalation recommended**: Yes/No — for P1/P2 always recommend human escalation

### 9. Remember

```
memory_store:
  tags: [incident, <severity>, <affected-service>, <date>]
  content: |
    Incident at <timestamp>: <summary>.
    Severity: <P1-P4>.
    Root cause: <identified or unknown>.
    Resolution: <what fixed it>.
    Duration: <time from detection to resolution>.
    Action items: <follow-up work needed>.
```

---

## Incident Report Template

```markdown
# Incident Report — <DATE> <TIME>

## Summary
<1-2 sentence plain-language description of what happened>

## Severity: P<1-4>
## Status: Active | Mitigated | Resolved

## Timeline

| Time     | Event                                    |
|----------|------------------------------------------|
| HH:MM    | First error detected in logs             |
| HH:MM    | Alert triggered / User report received   |
| HH:MM    | Investigation started                    |
| HH:MM    | Root cause identified                    |
| HH:MM    | Mitigation applied                       |
| HH:MM    | Service restored                         |

## Impact

- **Users affected**: <all / subset / internal only>
- **Services affected**: <list>
- **Data impact**: <none / potential loss / confirmed loss>
- **Duration**: <time from start to resolution>

## Root Cause

<Description of what caused the incident. If unknown, state what has been ruled out.>

## Mitigation Applied

<Step-by-step description of what was done to restore service>

## Diagnostic Data

<Key log excerpts, metrics, error messages that support the analysis>

## Action Items

- [ ] <Follow-up task 1>
- [ ] <Follow-up task 2>
- [ ] <Preventive measure>

## Lessons Learned

- <What could have prevented this>
- <What can be improved in detection/response>
```

---

## Common Root Causes Checklist

When investigating, check these common causes in order:

1. **Recent deployment** — Was anything deployed in the last 24 hours?
2. **Resource exhaustion** — Disk full, memory full, connection pool exhausted?
3. **External dependency** — Is a third-party service down?
4. **Configuration change** — Were env vars, secrets, or config files modified?
5. **Certificate expiry** — Have any TLS certs or API keys expired?
6. **Infrastructure** — EC2 instance health, network connectivity, DNS resolution?
7. **Data issue** — Corrupted data, schema migration failure, full table?
8. **Traffic spike** — Unusual load or potential DDoS?
