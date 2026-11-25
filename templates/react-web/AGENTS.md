# Working in this repository

Conventions for anyone changing this web app, people and coding agents alike. CI enforces the
parts marked **(CI)**.

## Stack

React 19, TypeScript (strict), Vite, React Router, TanStack Query, Tailwind CSS, Microsoft
Entra ID sign-in (MSAL). The API client is generated from the backend's `openapi.json`. Unit
tests: Vitest + Testing Library + MSW. Acceptance tests: Gherkin features run in Chromium by
Playwright (playwright-bdd) against a fake API.

## Layout

```
src/
  main.tsx, App.tsx      startup and providers (auth, query cache, router)
  router.tsx             every page; one route per feature
  config/env.ts          every VITE_* setting, validated at startup
  auth/                  useAuth(), <RequireAuth roles>, MSAL and test sign-in
  api/client.ts          typed API client (openapi-fetch) + ApiError
  api/openapi.json       the backend's contract, copied by `npm run api:pull`
  api/schema.d.ts        GENERATED from the backend's openapi.json: never edit by hand
  components/            shared UI (Button, Field, AppLayout...)
  features/<name>/       one folder per feature
    <name>.api.ts            TanStack Query hooks over the API client
    <Name>Page.tsx           the page and its parts
    <Name>Page.test.tsx      unit tests (MSW mocks the API)
  test/                  test setup, MSW server, renderWithProviders
features/                Gherkin acceptance tests and their steps
```

`src/features/notes` and `features/EXAMPLE-1-notes.feature` are the reference. Copy their shape.

## Rules

- **Server data** goes through TanStack Query hooks in `<feature>.api.ts` that call `api`
  (typed paths and bodies) through `call()`. No `fetch` in components. After a change, invalidate
  the queries it affects.
- **API contract.** `src/api/openapi.json` is the backend's contract. When the backend changes,
  run `OPENAPI_SOURCE=<path or URL> npm run api:generate` (copies it, then regenerates the client).
  Every acceptance scenario checks the fake API against it (`features/support/contract.ts`): a path
  the backend does not have, a request body it would reject or a fake response it would never send
  fails the scenario with the reason. Fix the fake (or the app), never the check. TS Harness points
  `OPENAPI_CONTRACT` at the backend's current `openapi.json` when it verifies a change. Fix the type errors that follow; they are
  the places the change affects.
- **Errors.** API failures arrive as `ApiError` (status, code, message). Show the message near
  what failed; never leave a rejected promise unhandled (use `mutate` with callbacks, or catch).
- **Auth.** Pages sit under `AppLayout` (signed in). Use `<RequireAuth roles={['Admin']}>` or
  `hasRole(useAuth(), ...)` for role-specific UI. The API enforces roles too; the UI only hides.
- **Accessibility.** Every input has a label (`Field`), buttons have text, headings are in
  order, status messages use `role="alert"` when they need attention. Tests find elements by
  role and label, so this is also what keeps tests working.
- **Styling.** Tailwind utilities; design tokens (colors, type, spacing) in `src/index.css`
  under `@theme`, taken from Figma. Match the Figma frame the ticket links: layout at 1440 px
  wide, copy, states (empty, loading, error).
- **Configuration.** Only `VITE_*` variables, read through `env`. They are public: never put
  secrets in them. A new one goes in `src/config/env.ts` and `.env.example`.
- **Formatting and lint (CI).** `npm run format` and `npm run lint` before committing.

## Roles

The app roles are **Admin** and **User** (Entra ID app roles, in the ID token's `roles` claim).
Use `<RequireAuth roles={['Admin']}>` or `hasRole(useAuth(), 'Admin')` to show admin-only UI;
the API enforces the same roles.

When a ticket defines different or additional roles, change them in one go: the components that
check roles, the personas in `features/support/fixtures.ts`, the scenarios that sign in as them,
and this section. Role names must match the API's and Entra ID's.

## Tests

- **Acceptance (BDD, CI).** Every ticket with acceptance criteria adds or updates a feature file
  named after the ticket key: `features/PROJ-14-week-view.feature`. One scenario per
  Given/When/Then criterion, in the ticket's words. Reuse the steps in
  `features/steps/common.steps.ts` (sign in as a persona, seed the API, open a page, fill, click,
  see). Seed data with `Given the API has <table>:` and a data table. When a page calls an
  endpoint the fake API does not know, extend `FakeApi` in `features/support/fixtures.ts` with
  the backend's real paths, bodies and error shape.
- **Unit (CI).** Pages and components with logic get Testing Library tests with MSW handlers
  for the API (`server.use(http.get(...))`): the normal case, loading, empty and error states.
  `npm test` enforces at least 80% line coverage of `src/features` and `src/components` (75% of
  branches); a change that adds UI logic without tests fails it. The Gherkin suite covers whole
  journeys in a browser; unit tests cover the states and edge cases.
- **Smoke (nightly).** `features/smoke/` holds the few features tagged `@smoke` that
  `.github/workflows/nightly-smoke.yml` runs each night against the real API and database
  (never in the normal suite): sign in through the API's CI-only test issuer, open every static
  route in `src/router.tsx`, one write. Every real API call is checked: a failed call, a
  response the contract does not allow or a page error fails the run, and TS Harness proposes a
  bug. A new page is covered automatically once it is in the router; give it seed data in the
  API's `scripts/smoke-seed.ts` so it has something to show. Do not add `@smoke` to ticket
  features.
- Run locally: `npm test`, and `npx playwright install chromium` once, then `npm run test:bdd`.

## Commands

| Command                                                       | What it does                                                            |
| ------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `npm run dev`                                                 | app on http://localhost:5173 (needs `.env.local`, see `.env.example`)   |
| `npm test`                                                    | unit tests                                                              |
| `npm run test:bdd`                                            | Gherkin acceptance tests in Chromium                                    |
| `npm run lint` / `npm run typecheck` / `npm run format:check` | static checks                                                           |
| `npm run api:generate`                                        | regenerate `src/api/schema.d.ts` from the backend's `openapi.json`      |
| `npm run build`                                               | production build into `dist/`                                           |
| `npm run test:smoke`                                          | `@smoke` features against a real API (`SMOKE_API_URL`, `SMOKE_API_DIR`) |
