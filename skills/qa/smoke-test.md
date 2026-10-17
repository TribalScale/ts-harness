# Smoke Test — Quick Validation Suite

## When to Use

Use this skill when:
- After a deployment completes (staging or production)
- The orchestrator requests environment validation
- A user says "run smoke tests" or "validate the deployment"
- As part of an automated post-deploy pipeline

Do **not** use this skill for:
- Full regression testing (run the complete test suite instead)
- Coverage analysis (use `coverage-check.md`)
- Load/performance testing

---

## Steps

### 1. Identify Target Environment

Confirm the target before running any tests:
- **Environment**: staging / production / local
- **Base URL**: e.g., `https://staging.ts-harness.example.com` or `http://localhost:3000`
- **Expected version**: commit SHA or tag if available

If the target is not specified, ask the orchestrator to confirm.

### 2. Run Connectivity Tests

Verify basic HTTP reachability for all critical endpoints:

```bash
ENDPOINTS=(
  "/health"
  "/api/health"
  "/"
)

BASE_URL="<target-url>"

for endpoint in "${ENDPOINTS[@]}"; do
  STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "${BASE_URL}${endpoint}" 2>/dev/null || echo "000")
  TIME=$(curl -sf -o /dev/null -w "%{time_total}" "${BASE_URL}${endpoint}" 2>/dev/null || echo "timeout")
  echo "${endpoint}: HTTP ${STATUS} (${TIME}s)"
done
```

**Pass criteria:**
- All endpoints return HTTP 2xx
- Response time < 5 seconds per endpoint

### 3. Run Playwright Smoke Suite

If a Playwright smoke test suite exists:

```bash
npx playwright test --grep @smoke --reporter=json 2>&1
```

Parse the results for pass/fail/skip counts.

If Playwright is not configured, skip this step and note it in the report.

### 4. Validate Key User Flows

Manually or programmatically verify these minimum flows:

#### Minimum Smoke Test Checklist

| Test                          | Method                                           | Pass Criteria              |
|-------------------------------|--------------------------------------------------|----------------------------|
| Homepage loads                | `GET /` returns 200                              | HTTP 200, page renders     |
| Health endpoint               | `GET /health` or `/api/health`                   | HTTP 200, body contains "ok" |
| Authentication (if applicable)| POST login with test credentials                 | HTTP 200, returns token    |
| API responds                  | `GET /api/<primary-resource>`                    | HTTP 200, valid JSON       |
| Database connected            | Health endpoint confirms DB connectivity         | Status field shows connected |
| Static assets load            | `GET /favicon.ico` or main CSS/JS bundle         | HTTP 200                   |

### 5. Capture Evidence on Failure

For any test failure:

**If using Playwright:**
- Screenshots are auto-captured to `test-results/` directory
- Record the screenshot path in the report

**If using curl/manual:**
- Capture the response body: `curl -s <url> > /tmp/smoke-fail-<endpoint>.txt`
- Capture error headers: `curl -sI <url> > /tmp/smoke-fail-<endpoint>-headers.txt`

### 6. Compile Results

Count results across all test methods:

```
Total:   <N>
Passed:  <N>
Failed:  <N>
Skipped: <N>
```

### 7. Build Failure Details

For each failure, include:
- **Test name**: Which test failed
- **Error message**: The actual error or unexpected status
- **Expected vs Actual**: What was expected vs what happened
- **Screenshot path**: If available
- **Reproduction steps**: How to manually reproduce

### 8. Return to Orchestrator

Provide the structured test report:
- **Overall status**: PASS (all green) / FAIL (any failures) / PARTIAL (skipped tests)
- **Summary counts**: passed/failed/skipped
- **Environment**: which environment was tested
- **Duration**: total test execution time
- **Failures**: detailed list if any
- **Recommendation**: deploy is safe / investigate failures / rollback recommended

---

## Test Report Template

```markdown
# Smoke Test Report — <TIMESTAMP>

## Environment: <staging | production | local>
## Base URL: <url>
## Overall: PASS | FAIL | PARTIAL

## Results

| Test                    | Status | Duration | Notes             |
|-------------------------|--------|----------|-------------------|
| Homepage loads          | PASS   | 0.3s     |                   |
| Health endpoint         | PASS   | 0.1s     |                   |
| Authentication          | PASS   | 0.5s     |                   |
| API response            | FAIL   | 2.1s     | Returned 500      |
| Database connected      | PASS   | 0.1s     |                   |
| Static assets           | PASS   | 0.2s     |                   |

**Passed**: 5 / 6
**Failed**: 1 / 6
**Skipped**: 0

## Failures

### API response — FAIL
- **Error**: HTTP 500 Internal Server Error
- **Expected**: HTTP 200 with valid JSON
- **Response body**: `{"error": "relation \"contracts\" does not exist"}`
- **Screenshot**: N/A (API test)
- **Reproduction**: `curl -s https://staging.ts-harness.example.com/api/contracts`

## Recommendation

Investigate API failure before promoting to production. Database migration may not have run.
```

---

## Failure Report Template (per failure)

```markdown
### <Test Name> — FAIL

- **Error**: <error message or unexpected status>
- **Expected**: <what should have happened>
- **Actual**: <what actually happened>
- **Screenshot**: <path or N/A>
- **Reproduction**: <curl command or steps>
- **Possible cause**: <hypothesis if obvious>
```
