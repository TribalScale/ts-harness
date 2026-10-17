# Release Notes — Generate from Merged PRs

## When to Use

Use this skill when:
- The orchestrator requests release notes for a version or time period
- A new release is being cut and needs changelog documentation
- A user asks "what shipped this week/sprint/release?"

Do **not** use this skill for:
- Sprint reports with velocity metrics (use `sprint-report.md`)
- Incident reports (use `incident-summary.md`)

---

## Steps

### 1. Fetch Merged PRs

Determine the range and fetch merged PRs.

**By date range:**

```bash
gh pr list --state merged --search "merged:>=YYYY-MM-DD merged:<=YYYY-MM-DD" --limit 100 --json number,title,labels,author,mergedAt,body
```

**Since last tag:**

```bash
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
if [ -n "$LAST_TAG" ]; then
  git log ${LAST_TAG}..HEAD --merges --oneline
fi
gh pr list --state merged --search "merged:>=$(git log -1 --format=%ai $LAST_TAG | cut -d' ' -f1)" --limit 100 --json number,title,labels,author,mergedAt,body
```

### 2. Categorize Each PR

Classify every PR into exactly one category based on its title prefix, labels, or content:

| Category           | Matching Criteria                                                   |
|--------------------|---------------------------------------------------------------------|
| **Features**       | Title starts with `feat`, label `enhancement` or `feature`          |
| **Bug Fixes**      | Title starts with `fix`, label `bug` or `bugfix`                    |
| **Performance**    | Title starts with `perf`, label `performance`                       |
| **Refactoring**    | Title starts with `refactor`, label `refactor` or `tech-debt`       |
| **Documentation**  | Title starts with `docs`, label `documentation`                     |
| **Infrastructure** | Title starts with `chore` or `ci`, label `infra` or `devops`       |
| **Breaking Changes** | Body contains `BREAKING CHANGE`, label `breaking`                |

If a PR does not match any prefix or label, infer the category from the PR body and changed files. When ambiguous, default to **Infrastructure**.

### 3. Group by Category

Organize PRs into their categories. Within each category, sort by merge date (newest first).

### 4. Write Human-Readable Descriptions

For each PR, write a concise one-line description:
- Focus on *what changed for the user*, not implementation details
- Use active voice: "Add...", "Fix...", "Improve..."
- Include the PR number as a reference

**Good:** "Add JWT token refresh to prevent session expiry (#142)"
**Bad:** "Refactored the auth module to use a new token service pattern with dependency injection (#142)"

### 5. Flag Breaking Changes

If any PR includes breaking changes:
- Pull them into a dedicated **Breaking Changes** section at the top
- Describe what breaks and what the migration path is
- Reference the PR for full details

### 6. Format as Markdown

Use the output template below.

### 7. Return to Orchestrator

Provide:
- The formatted release notes
- Total PR count and category breakdown
- Any PRs that were hard to categorize (for human review)

---

## Output Template

```markdown
# Release Notes — v<VERSION> (<DATE>)

## Breaking Changes

> **None** (or list breaking changes with migration instructions)

## Features

- Add <description> (#<PR>) — @<author>
- Add <description> (#<PR>) — @<author>

## Bug Fixes

- Fix <description> (#<PR>) — @<author>

## Performance

- Improve <description> (#<PR>) — @<author>

## Refactoring

- Refactor <description> (#<PR>) — @<author>

## Documentation

- Update <description> (#<PR>) — @<author>

## Infrastructure

- Update <description> (#<PR>) — @<author>

---

**Full Changelog**: <compare URL or commit range>
**Contributors**: @<list of unique authors>
```

---

## Example

```markdown
# Release Notes — v1.4.0 (2026-03-28)

## Breaking Changes

> **None**

## Features

- Add bulk document upload via drag-and-drop (#187) — @alice
- Add email notification preferences to user settings (#183) — @bob

## Bug Fixes

- Fix contract search returning stale results after edit (#191) — @carol
- Fix PDF export crashing on documents with embedded images (#188) — @alice

## Performance

- Improve clause search response time by 40% with index optimization (#190) — @dave

## Infrastructure

- Update Node.js base image to 20.11 LTS (#189) — @bob

---

**Full Changelog**: v1.3.0...v1.4.0
**Contributors**: @alice, @bob, @carol, @dave
```
