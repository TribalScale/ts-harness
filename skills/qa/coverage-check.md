# Coverage Check — Test Coverage Analysis

## When to Use

Use this skill when:
- After code changes land on a branch or main
- The orchestrator requests a coverage report
- Before a release to verify coverage meets minimum thresholds
- A user asks "what's our test coverage?"

Do **not** use this skill for:
- Running smoke tests (use `smoke-test.md`)
- Code review (use `code-review.md`)
- Running tests without coverage analysis (just run `npm test`)

---

## Steps

### 1. Recall Coverage Baseline

Query memory for the previous coverage baseline:

```
memory_recall: tags=[coverage, baseline, <repo-name>]
```

Look for:
- Last recorded coverage percentages (line, branch, function, statement)
- Minimum coverage thresholds established for this project
- Areas previously flagged as under-covered

If no baseline exists in memory, this run establishes the initial baseline.

### 2. Run Tests with Coverage

Execute the test suite with coverage instrumentation:

**Node.js / Jest:**

```bash
npm test -- --coverage --coverageReporters=json-summary --coverageReporters=text 2>&1
```

**Node.js / Vitest:**

```bash
npx vitest run --coverage 2>&1
```

**Python / pytest:**

```bash
pytest --cov=. --cov-report=term-missing --cov-report=json 2>&1
```

Capture both the human-readable output and structured data (JSON) if available.

### 3. Parse Coverage Output

Extract the four key metrics:

| Metric         | Description                           | Target  |
|----------------|---------------------------------------|---------|
| **Lines**      | Percentage of executable lines covered | >= 80%  |
| **Branches**   | Percentage of decision branches covered| >= 75%  |
| **Functions**  | Percentage of functions called         | >= 80%  |
| **Statements** | Percentage of statements executed      | >= 80%  |

Also extract per-file coverage for files below threshold.

### 4. Compare Against Baseline

Calculate the delta for each metric:

```
Line coverage:      82.4% (was 81.1%) → +1.3%
Branch coverage:    76.2% (was 77.0%) → -0.8%
Function coverage:  85.0% (was 84.5%) → +0.5%
Statement coverage: 83.1% (was 82.0%) → +1.1%
```

### 5. Identify Uncovered Critical Paths

Flag files and paths that are both:
- Below the minimum threshold, AND
- In critical areas of the codebase (auth, data access, payment, API handlers)

Prioritize these for attention:

| File                      | Line % | Branch % | Critical? | Action Needed    |
|---------------------------|--------|----------|-----------|------------------|
| `src/auth/token.ts`       | 45%    | 30%      | Yes       | Add tests urgently |
| `src/api/contracts.ts`    | 72%    | 65%      | Yes       | Improve coverage  |
| `src/utils/format.ts`     | 60%    | 55%      | No        | Nice to have      |

### 6. Flag Coverage Decreases

Any coverage decrease > 2% in any metric is a warning:

- **> 2% decrease**: Warning — flag for attention
- **> 5% decrease**: Critical — likely missing tests for new code
- **Below minimum threshold**: Critical — coverage does not meet project standards

### 7. Compile Coverage Report

Use the template below to build the structured report.

### 8. Return to Orchestrator

Provide:
- **Overall verdict**: PASS (all metrics above threshold) / WARN (decrease detected) / FAIL (below minimum)
- **Summary line**: e.g., "Coverage at 82.4% lines (+1.3%), all thresholds met"
- **Full report**: Detailed breakdown
- **Action items**: List of files needing coverage improvement

### 9. Remember New Baseline

Store the updated baseline:

```
memory_store:
  tags: [coverage, baseline, <repo-name>, <date>]
  content: |
    Coverage baseline as of <date>:
    Lines: <X>%, Branches: <X>%, Functions: <X>%, Statements: <X>%
    Delta from previous: Lines <+/-X>%, Branches <+/-X>%
    Files below threshold: <list or "none">
    Overall: <PASS/WARN/FAIL>
```

---

## Coverage Thresholds

| Metric     | Minimum | Target  | Excellent |
|------------|---------|---------|-----------|
| Lines      | 80%     | 85%     | 90%+      |
| Branches   | 75%     | 80%     | 85%+      |
| Functions  | 80%     | 85%     | 90%+      |
| Statements | 80%     | 85%     | 90%+      |

These thresholds can be overridden per-project in the project's configuration or `RULES.md`.

---

## Coverage Report Template

```markdown
# Coverage Report — <DATE>

## Overall Verdict: PASS | WARN | FAIL

## Summary

| Metric     | Current | Previous | Delta   | Threshold | Status |
|------------|---------|----------|---------|-----------|--------|
| Lines      | 82.4%   | 81.1%    | +1.3%   | 80%       | PASS   |
| Branches   | 76.2%   | 77.0%    | -0.8%   | 75%       | PASS   |
| Functions  | 85.0%   | 84.5%    | +0.5%   | 80%       | PASS   |
| Statements | 83.1%   | 82.0%    | +1.1%   | 80%       | PASS   |

## Files Below Threshold

| File                    | Lines | Branches | Functions | Priority  |
|-------------------------|-------|----------|-----------|-----------|
| src/auth/token.ts       | 45%   | 30%      | 50%       | Critical  |
| src/api/contracts.ts    | 72%   | 65%      | 78%       | High      |

## Coverage Trend

- Previous: 81.1% lines (recorded <date>)
- Current: 82.4% lines
- Direction: Improving

## Recommendations

- <Specific files or modules that need test coverage>
- <Any patterns of untested code>
```
