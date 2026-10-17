# Code Review — Structured PR Review

## When to Use

Use this skill when:
- The orchestrator assigns a PR for review
- A user asks you to review a pull request by number or URL
- You are part of a review rotation and a PR is ready for review

Do **not** use this skill for:
- Implementing code (use `jira-to-pr.md`)
- Reviewing deployment artifacts (use ops skills)

---

## Steps

### 1. Recall Context

Query memory for project conventions and past review patterns:

```
memory_recall: tags=[conventions, code-review, style-guide]
```

Look for:
- Project-specific coding conventions
- Past review feedback on similar code
- Known anti-patterns in this codebase
- Team preferences (e.g., preferred libraries, patterns)

### 2. Fetch PR Diff

Retrieve the PR details and diff:

```bash
gh pr view <PR_NUMBER> --json title,body,files,additions,deletions,author
gh pr diff <PR_NUMBER>
```

Understand the full scope before reviewing individual files.

### 3. Apply Review Checklist

Evaluate each changed file against every item in the checklist. Do not skip categories.

#### Review Checklist

| Category        | What to Check                                                        |
|-----------------|----------------------------------------------------------------------|
| **Correctness** | Logic is correct, edge cases handled, no off-by-one errors, null safety |
| **Security**    | No secrets in code, input validation, SQL injection prevention, auth checks, OWASP top 10 |
| **Performance** | No N+1 queries, efficient algorithms, no unnecessary re-renders, appropriate caching |
| **Style**       | Follows project conventions, consistent naming, no dead code, DRY principles |
| **Tests**       | Adequate coverage, meaningful assertions, edge cases tested, no flaky patterns |
| **Documentation** | Public APIs documented, complex logic explained, README updated if needed |
| **Dependencies** | New deps justified, no known vulnerabilities, license compatible      |
| **Architecture** | Follows established patterns, appropriate separation of concerns, no unnecessary coupling |

### 4. Classify Findings

Every finding must have a severity:

| Severity       | Icon | Meaning                                              | Blocks Merge? |
|----------------|------|------------------------------------------------------|---------------|
| **Critical**   | :red_circle:    | Bug, security issue, data loss risk, broken functionality | Yes           |
| **Warning**    | :yellow_circle:    | Performance concern, code smell, missing tests       | Discuss        |
| **Suggestion** | :blue_circle:    | Style improvement, optional refactor, nice-to-have   | No            |
| **Praise**     | :green_circle:    | Good pattern, clean implementation, clever solution   | N/A           |

### 5. Write Review Comments

For each finding, comment on the specific line(s) in the PR:

**Comment format:**

```
[SEVERITY] Category — Brief title

<Explanation of the issue>

<Suggested fix or alternative, with code if helpful>
```

**Example:**

```
[Critical] Security — Unsanitized user input in SQL query

The `userId` parameter is interpolated directly into the SQL string
on line 42, which allows SQL injection.

Suggested fix:
- Use parameterized queries:
  `db.query('SELECT * FROM users WHERE id = $1', [userId])`
```

Rules for good review comments:
- Be specific — reference exact lines
- Explain *why* something is a problem, not just *what*
- Offer a concrete fix or alternative when possible
- Be respectful and constructive
- Acknowledge good work with praise comments

### 6. Provide Overall Summary

After individual comments, write an overall review summary:

```markdown
## Review Summary

**Verdict**: [Approve | Request Changes | Comment]

### Overview
<2-3 sentences summarizing the PR quality and key findings>

### Critical Issues (must fix)
- <list or "None">

### Warnings (should discuss)
- <list or "None">

### Suggestions (optional improvements)
- <list or "None">

### Highlights
- <positive things worth noting>

### Test Coverage Assessment
<comment on whether tests are adequate>
```

**Verdict rules:**
- **Approve**: No critical issues, warnings are minor
- **Request Changes**: Any critical issue exists, or multiple related warnings
- **Comment**: Only suggestions and questions, no blocking concerns

### 7. Return to Orchestrator

Provide a structured response:
- **PR number and title**
- **Verdict**: approve / request changes / comment
- **Critical count**: number of critical findings
- **Warning count**: number of warnings
- **Suggestion count**: number of suggestions
- **One-line summary**: the most important thing about this PR

### 8. Remember

Store any new patterns or conventions discovered:

```
memory_store:
  tags: [code-review, conventions, <component-area>]
  content: |
    Reviewed PR #<number>: <title>.
    Verdict: <verdict>.
    Notable patterns: <any new conventions or anti-patterns found>.
    Key feedback: <most important review point>.
```

---

## Review Etiquette

- Assume good intent from the author
- Ask questions instead of making demands when intent is unclear
- Distinguish between personal preference and objective issues
- Timebox the review — aim for 30-60 minutes max per PR
- If the PR is too large (>500 lines), suggest splitting it
