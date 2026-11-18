# Repository templates for empty repositories

A new project usually starts with empty repositories: a README, maybe a `.gitignore` or LICENSE,
nothing else. Coding jobs in an empty repository have no structure to follow, no test runner and
no CI, and parallel tickets each invent their own. TS Harness instead starts each empty
repository from a tested template kept in this repo (`templates/`).

| Template | Repository roles | Stack |
|---|---|---|
| `node-api` | api, backend, server, service | Express 5, TypeScript, Postgres (Prisma), Entra ID tokens, Zod + OpenAPI, Vitest, Cucumber BDD, CI, Docker |
| `react-web` | web, frontend, ui, spa, app | React 19, Vite, Entra ID sign-in (MSAL), typed client from the API's `openapi.json`, Tailwind, Vitest, Gherkin via Playwright, CI, Docker |

Both come with an `AGENTS.md` (the conventions coding agents follow), a reference module with
unit and BDD tests, GitHub Actions CI with the jobs `checks`, `bdd` and `docker`, and the app
roles `Admin` and `User`.

## How it works

1. **Planning.** When a program's project has an empty repository whose role has a template,
   `project_repositories` carries `template` and `template_conventions` (the template's
   `AGENTS.md`) for it. The planner plans on top of the template: its folders and conventions,
   no scaffolding or CI work orders.
2. **Bootstrap PR.** `implement all <PROJECT>` checks the project's repositories before the
   queue runs (`prepare_ticket_queue`). Each empty one gets a PR into its base branch from
   `ts-harness/bootstrap-<template>`: the template as one commit, with the project's name filled
   in, and `.ts-harness.yml` recording `template: <name>`. The queue stops and lists the PRs. A
   single ticket (`implement JMT-12`) does the same check for its repository
   (`prepare_ticket_delivery`). Asking again while the PR is open points at the same PR.
3. **Review and merge.** The PR's own CI runs; review it like any other PR (the harness checks
   report "Not a TS Harness PR"). Then run the queue or ticket again.
4. **Required checks.** The first time a ticket or queue sees the repository after the merge,
   the template's CI jobs become required checks on the base branch (the same protection
   `scripts/repo-governance.py --apply` sets). The ticket summary notes it.

Only repositories that hold nothing but starter files are touched (`README*`, `LICENSE*`,
`.gitignore`, `.gitattributes`, `.editorconfig`, `CODEOWNERS`). A repository with any other file
is never bootstrapped. A repository with no role, or a role no template serves, is left alone.

## Prerequisites

- The repository exists with its base branch (create it with a README, then the branch). With
  no base branch the step blocks and says so.
- The GitHub App is installed on it with **Contents**, **Pull requests**, **Workflows** (to add
  `.github/workflows/ci.yml`) and **Administration** (to require the checks after the merge).
- The project (System → Projects) lists the repository with its **role** (`api`, `web`...).

## Settings

`program_defaults.repo_templates` (System → Settings):

```json
{ "repo_templates": { "enabled": true, "by_role": { "worker": "node-api", "web": "none" } } }
```

| Key | Default | Effect |
|---|---|---|
| `enabled` | `true` | `false` turns bootstrapping and the planning annotation off |
| `by_role` | the roles in each `template.json` | Adds or overrides role → template; `"none"` turns templates off for that role |

## Changing a template

Templates are complete repositories. CI runs each one's checks (`template-node-api`,
`template-react-web` jobs), so a template that stops passing fails the harness build. To change
one: edit it under `templates/<name>`, run `npm ci`, `npm run lint`, `npm run typecheck`,
`npm test`, `npm run test:bdd` there (Node 22 or 24), and bump `version` in `template.json`.
Repositories already started from an older version are not updated.

`template.json` lists the roles it serves, the CI jobs to require (`required_checks`) and the
text replaced when applied: `{package_name}` (the repository name as an npm name) and
`{display_name}` (the project's name, else the repository name).

## Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| "has no `dev` branch" | The base branch does not exist | Create it, then run again |
| GitHub 403 when opening the PR | The App lacks Workflows or Contents write, or is not installed on the repository | Update the installation's permissions or repository list |
| "could not require the template's CI checks" | No Administration permission | Run `scripts/repo-governance.py <repo> --branch <base> --apply` as an admin |
