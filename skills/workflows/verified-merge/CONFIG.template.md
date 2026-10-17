# Configuration: verified-merge

Copy this to your project (e.g. `.cursor/verified-merge-config.md`) and fill in the blanks before using the workflow.

## Project Identity

- **Project name**:
- **Tech stack**:
- **Base branch**: main

## Delivery Source

- **Default source**: work-order | jira
- **Auto-detect from input**: yes
- **Sync on events**: context_accepted, in_development, pr_opened, merged, post_merge_verified
- **Mirror status to Work Order markdown**: no

## Branch Convention

- **Branch prefix**: feature
- **Branch pattern**: `{prefix}-{unit-id}-{short-description}`
- **Examples**: `wo-003-user-export`, `proj-123-user-export`

## Merge Strategy

- **Default**: squash (`gh pr merge --squash`)
- **Require explicit user approval**: yes
- **Never force-push to base branch**: yes

## Verification Commands

- **Local tests**: npm test
- **Type check / build**: npm run build
- **Lint**: npm run lint
- **Post-merge verification**: git checkout <base-branch> && git pull && npm test

## Verification Audits

Optional risk-based audits invoked at Step 6. See `skills/verify/audit-report-contract.md`.

- **Audits to run** (`verify.audits`): none | security | dependency | accessibility | performance | test (comma-separated or list)
- **Blocking severity** (`verify.blocking_severity`): critical, high (default)
- **Scope**: PR diff and changed paths by default

Examples:

- `verify.audits: security, dependency, test` — run three audits before PR
- `verify.audits: none` or unset — skip audits (default)

Audits report only; this workflow fixes findings and gates merge.

## Operational Readiness

Optional readiness check invoked at Step 6. See `skills/operate/operational-readiness-contract.md`.

- **Readiness gate** (`verify.readiness`): on | off (default off)
- **Trigger heuristics**: new service, new deploy surface, or explicit config enable
- **Blocking severity**: critical, high (same default as audits)

Examples:

- `verify.readiness: on` — run operational readiness before merge for configured units
- `verify.readiness: off` or unset — skip readiness (default)

Readiness reports only; this workflow addresses gaps and gates merge.

## Build Scaffolds

Optional scaffold resolution at Step 5 before TDD. See `skills/build/build-scaffold-contract.md`.

- **Scaffold mode** (`build.scaffolds`): ask | auto | off (default ask)
- **Registered scaffolds**: `ts-node-backend-scaffold`, `ts-frontend-scaffold`, `ts-cicd-scaffold`
- **Rule**: at most one scaffold per unit; ambiguity escalates to user

Examples:

- `build.scaffolds: ask` — detect match, confirm, then invoke scaffold before TDD
- `build.scaffolds: auto` — invoke on single detect match without confirmation
- `build.scaffolds: off` — skip scaffold resolution (default TDD-only behavior)

Scaffolds produce structure; TDD implements behavior. Scaffolds do not gate merge.

## Forbidden Patterns

- Do not merge without explicit user approval
- Do not use `--no-verify` or `--no-gpg-sign` on commits
- Do not mark a unit complete before post-merge verification
- Do not edit upstream planning manifests (`docs/prd-to-work-orders/manifest.json`)
- Do not invent external tracker transitions or status names

## Escalation

- **Stop if**: acceptance criteria are ambiguous or untestable
- **Stop if**: the unit spans multiple independent subsystems (propose split first)
- **Stop if**: Work Order dependencies are not Done
- **Ask before**: merging with non-green CI checks
- **Ask before**: force-push or skipping hooks

## Jira Adapter Settings

Configure these when using `ts-source-jira`. See the `CONFIG.template.md` in the `ts-source-jira` adapter (integrate category).

- **Ticket key pattern**: PROJ-###
- **In Development status**: In Development | In Progress
- **Completion status**: Done | Ready for QA | DEV COMPLETE
- **Jira MCP server**: user-mcp-atlassian
