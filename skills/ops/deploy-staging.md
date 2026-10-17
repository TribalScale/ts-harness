# Deploy to Staging — Controlled Deployment

## When to Use

Use this skill when:
- The orchestrator requests a staging deployment
- A user says "deploy to staging" or "push to staging"
- After a PR merge when the user explicitly asks to deploy the merged code to staging

Do **not** use this skill for:
- Production deployments (requires separate production skill with additional gates)
- Local development environment setup
- Health checks only (use `health-check.md`)

---

## Steps

### 1. Recall Deployment History

Query memory for recent deployments:

```
memory_recall: tags=[deployment, staging, recent]
```

Look for:
- Last successful deployment (when, what version)
- Any recent deployment failures and their causes
- Known deployment quirks or manual steps

### 2. Pre-flight Checks

Run through the pre-flight checklist before proceeding:

| Check                        | Command / Action                          | Required |
|------------------------------|-------------------------------------------|----------|
| Tests passing on main        | `gh run list --branch main --limit 1`     | Yes      |
| No blocking issues           | Check Jira for P1/P2 blockers             | Yes      |
| Branch is up to date         | `git log origin/main..HEAD` should be empty | Yes    |
| No active incidents          | `memory_recall: tags=[incident, active]`  | Yes      |
| Disk space sufficient        | `df -h /vol/ts-harness-data` (> 20% free)   | Yes      |

If any required check fails, **stop** and report the failure to the orchestrator. Do not proceed.

### 3. USER REQUEST GATE

Staging deployments are allowed for `ops-agent`, but the deployment must be triggered by an explicit user request after the PR is merged. Do not deploy merely because a PR exists, QA passed, or a standing routine noticed a merge.

Present the deployment plan to the orchestrator before proceeding:

```
STAGING DEPLOYMENT PLAN

Target:      staging environment
Branch:      main (commit <SHA>)
Last deploy: <date> (commit <SHA>)
Changes:     <number> commits since last deploy

Pre-flight:  All checks passed

PRs included:
- #<num>: <title>
- #<num>: <title>

Estimated downtime: ~30 seconds (container restart)

Proceed with deployment? [yes/no]
```

Proceed only when the user request clearly authorizes staging deployment. If the orchestrator response is ambiguous, treat it as "no" and stop.

### 4. Pull Latest Code

```bash
cd /path/to/ts_harness
git checkout main
git pull origin main
```

Verify the checkout matches the expected commit SHA from the confirmation gate.

### 5. Build Containers

```bash
docker compose build --no-cache
```

If the build fails:
- Capture the error output
- Report to orchestrator immediately
- Do NOT proceed to the next step
- Tag as failed deployment in memory

### 6. Deploy

```bash
docker compose down
docker compose up -d
```

Record the deployment start timestamp.

### 7. Wait for Health Checks

Poll health checks for up to 2 minutes:

```bash
for i in $(seq 1 24); do
  STATUS=$(curl -sf -o /dev/null -w "%{http_code}" http://localhost:<PORT>/health 2>/dev/null || echo "000")
  if [ "$STATUS" = "200" ]; then
    echo "Health check passed after $((i * 5)) seconds"
    break
  fi
  echo "Attempt $i/24: status=$STATUS, waiting 5s..."
  sleep 5
done
```

If health checks do not pass within 2 minutes:
- Execute rollback procedure (see below)
- Report failure to orchestrator

### 8. Run Smoke Tests

If a smoke test suite is available:

```bash
npx playwright test --grep @smoke
```

If smoke tests fail:
- Capture failure details and screenshots
- Assess severity: is the core app functional?
- If critical failures, execute rollback
- Report results to orchestrator

### 9. Report Deployment Status

Provide the orchestrator with:
- **Status**: success / partial / failed / rolled back
- **Version**: commit SHA deployed
- **Health check**: pass/fail with response time
- **Smoke tests**: pass/fail count
- **Deployment duration**: time from start to healthy
- **Action needed**: none / investigate / rollback confirmed

### 10. Remember

```
memory_store:
  tags: [deployment, staging, <date>]
  content: |
    Deployed to staging: commit <SHA> at <timestamp>.
    Status: <success/failure>.
    PRs included: <list>.
    Health check: <pass/fail>.
    Smoke tests: <pass/fail>.
    Notes: <any issues encountered>.
```

---

## Rollback Procedure

If deployment fails after `docker compose up`:

```bash
# Option 1: Revert to previous image (if tagged)
docker compose down
git checkout <previous-known-good-sha>
docker compose build
docker compose up -d

# Option 2: Use Docker image cache
docker compose down
docker compose up -d --no-build  # uses cached images from before rebuild
```

After rollback:
1. Verify health checks pass
2. Report rollback to orchestrator
3. Store the failure in memory with root cause if identified

---

## Pre-flight Checklist (Quick Copy)

- [ ] CI green on main
- [ ] No P1/P2 blockers in Jira
- [ ] Branch up to date with origin/main
- [ ] No active incidents
- [ ] Disk space > 20% free
- [ ] Human approval received
