# Frontend template

A starting point for React web apps: Vite, TypeScript, React Router, TanStack Query, Tailwind,
Microsoft Entra ID sign-in, a typed API client generated from the backend's OpenAPI document,
Vitest unit tests, Gherkin acceptance tests in Playwright, GitHub Actions CI and a static Docker
image. Pairs with the backend template (same `notes` reference resource).

## Quick start

```bash
nvm use                  # Node 22
npm install
cp .env.example .env.local   # set the Entra ID client and API scope
npm run dev                  # http://localhost:5173
```

Tests: `npm test` (unit) and `npm run test:bdd` (acceptance; run `npx playwright install
chromium` once).

## Start a new app from it

1. Create the repository from this template, or copy it in as the first commit.
2. Rename `name` in `package.json` and the `<title>` in `index.html`.
3. Register the SPA in Entra ID (redirect URI = the app's origin) with permission to the API's
   scope. Set `VITE_AUTH_CLIENT_ID`, `VITE_AUTH_TENANT_ID`, `VITE_API_SCOPE`, `VITE_API_URL`.
4. Generate the API client from your backend: `OPENAPI_SOURCE=<path or URL> npm run api:generate`.
5. Put the Figma design tokens in `src/index.css` (`@theme`).
6. Replace the `notes` reference feature with your first real one once it exists.
7. Protect the base branch and require the CI jobs `checks`, `bdd` and `docker`.

See [AGENTS.md](AGENTS.md) for the conventions.

## Deploying

`npm run build` produces static files in `dist/`. On AWS, host them on S3 behind CloudFront
(route unknown paths to `index.html`), or run the Docker image (nginx on port 8080, `/health`).
`VITE_*` values are baked in at build time, so build once per environment.
