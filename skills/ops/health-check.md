# Health Check — System Status Assessment

## When to Use

Use this skill when:
- The orchestrator requests a system status check
- A daily heartbeat trigger fires
- Before or after a deployment to verify system state
- A user asks "is the system healthy?" or "what's the server status?"

Do **not** use this skill for:
- Active incident investigation (use `incident-summary.md`)
- Performance profiling or load testing

---

## Steps

### 1. Check Docker Container Status

```bash
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
```

**Evaluate:**
- All expected containers are running
- No containers in `restarting` or `exited` state
- Uptime is reasonable (not recently restarted unexpectedly)

### 2. Check Container Resource Usage

```bash
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}"
```

**Evaluate:**
- No container using > 90% memory
- CPU usage is within expected range
- No unusual network or block I/O spikes

### 3. Check Disk Usage

```bash
df -h /vol/ts-harness-data
du -sh /vol/ts-harness-data/* 2>/dev/null | sort -rh | head -10
```

**Evaluate:**
- Disk usage < 80% (yellow at 70%, red at 80%)
- No single directory consuming disproportionate space

### 4. Check Memory

```bash
free -h
```

**Evaluate:**
- Available memory > 10% of total
- Swap usage is minimal (< 20% of swap space)

### 5. Check Tailscale Status

```bash
tailscale status
```

**Evaluate:**
- Node is online and connected
- Peer connections are active
- No authentication warnings

### 6. Check Application Health Endpoint

```bash
curl -sf -o /dev/null -w "%{http_code} %{time_total}s" http://localhost:<PORT>/health || echo "UNREACHABLE"
```

**Evaluate:**
- HTTP 200 response
- Response time < 2 seconds
- If unreachable, flag as critical

### 7. Check Postgres

```bash
docker compose exec -T postgres pg_isready
docker compose exec -T postgres psql -U ts_harness -c "SELECT pg_database_size('ts_harness') AS db_size, numbackends AS active_connections FROM pg_stat_database WHERE datname = 'ts_harness';"
```

**Evaluate:**
- Postgres accepting connections
- Active connections within expected range (< 80% of max)
- Database size growth is reasonable

### 8. Compile Health Report

Aggregate all findings into the structured report below.

### 9. Flag Warnings

Apply thresholds and set status indicators:

| Metric               | Green            | Yellow            | Red                |
|----------------------|------------------|-------------------|--------------------|
| Container status     | All running      | Restart < 5 min ago | Any exited/unhealthy |
| CPU usage            | < 70%            | 70-90%            | > 90%              |
| Memory (container)   | < 70%            | 70-90%            | > 90%              |
| Memory (host)        | > 20% available  | 10-20% available  | < 10% available    |
| Disk usage           | < 70%            | 70-80%            | > 80%              |
| Tailscale            | Online           | —                 | Offline            |
| Health endpoint      | 200, < 1s        | 200, 1-2s         | Non-200 or > 2s    |
| Postgres             | Ready, < 50 conn | 50-80 conn        | Not ready or > 80  |

### 10. Return to Orchestrator

Provide the structured health report. If any metric is red, flag it prominently at the top of the response.

---

## Health Report Template

```markdown
# System Health Report — <TIMESTAMP>

## Overall Status: [GREEN | YELLOW | RED]

<If YELLOW or RED, list the concerning metrics here>

## Containers

| Container       | Status   | Uptime   | CPU    | Memory       |
|-----------------|----------|----------|--------|--------------|
| ts_harness-app    | Running  | 3d 4h   | 12%    | 450M / 1G    |
| ts-harness-db     | Running  | 3d 4h   | 3%     | 200M / 512M  |
| ts_harness-redis  | Running  | 3d 4h   | 1%     | 50M / 256M   |

## Host Resources

| Metric             | Value         | Status  |
|--------------------|---------------|---------|
| Disk usage         | 45% (22G/50G) | GREEN   |
| Host memory        | 1.2G free / 4G | GREEN  |
| Swap usage         | 0M / 2G       | GREEN   |

## Network

| Service      | Status    |
|--------------|-----------|
| Tailscale    | Online    |
| Health endpoint | 200 OK (0.12s) |

## Database

| Metric              | Value    | Status  |
|---------------------|----------|---------|
| Connection status   | Ready    | GREEN   |
| Active connections  | 12       | GREEN   |
| Database size       | 1.8 GB   | —       |

## Recommendations

- <any action items based on yellow/red indicators>
```
