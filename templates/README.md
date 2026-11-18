# Repository templates

Starting points TS Harness applies to **empty** project repositories (a README, `.gitignore` or
LICENSE and nothing else). The first time a program or ticket queue targets such a repository,
the harness opens a bootstrap PR with the template; feature work starts once it merges. See
[docs/runbooks/repo-templates.md](../docs/runbooks/repo-templates.md).

| Template | For repository roles | What it is |
|---|---|---|
| `node-api` | api, backend, server, service | Express 5, Postgres (Prisma), Entra ID, Zod + OpenAPI, Cucumber BDD |
| `react-web` | web, frontend, ui, spa, app | React 19, Vite, Entra ID (MSAL), typed API client, Tailwind, Gherkin via Playwright |

Each template is a complete, working repository: its own CI (`checks`, `bdd`, `docker`) runs in
this repo's CI too, so a template that stops passing fails the harness build.

`template.json` describes it: the roles it serves, the CI jobs to require once merged, and the
text replaced when it is applied (`{package_name}` is the repository name, `{display_name}` the
project's name). Change a template like any code here: run its checks (`npm ci`, `npm run lint`,
`npm run typecheck`, `npm test`, `npm run test:bdd`) and bump `version`.
