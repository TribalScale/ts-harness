# Working in this repository

Conventions for anyone changing this API, people and coding agents alike. CI enforces the parts
marked **(CI)**.

## Stack

Node 22, TypeScript (strict, ESM), Express 5, Postgres through Prisma, Zod for validation and
API docs, pino logs, Microsoft Entra ID access tokens. Unit tests: Vitest. Acceptance tests:
Cucumber (Gherkin) against the real API and a real database.

## Layout

```
src/
  app.ts               Express app: middleware, modules, docs, error handler
  server.ts            starts the server, graceful shutdown
  container.ts         shared dependencies (Deps) and the Module type
  config/env.ts        every environment variable, validated at startup
  config/openapi.ts    OpenAPI registry
  middlewares/         auth (authenticate, requireRole), validate, errorHandler
  errors/appError.ts   errors the API reports (status + code)
  modules/<name>/      one folder per resource
    <name>.schemas.ts      Zod schemas: request, response, types
    <name>.repository.ts   interface + Prisma implementation (all SQL lives here)
    <name>.service.ts      business rules (no HTTP, no Prisma)
    <name>.controller.ts   HTTP in/out only
    <name>.routes.ts       paths, auth, validation
    <name>.openapi.ts      registers the paths for /docs and openapi.json
    <name>.service.test.ts unit tests with a fake repository
    index.ts               wires the module; add it to src/modules/index.ts
features/              Gherkin acceptance tests
prisma/                schema and migrations
```

`src/modules/notes` is the reference module. Copy its shape for a new resource.

## Rules

- **Layers.** Routes → controller → service → repository. Controllers never touch Prisma;
  services never touch `req`/`res`. Pass dependencies through constructors.
- **Validation.** Every request body, query and path parameter goes through `validate()` with a
  Zod schema. Never trust `req.body` directly.
- **Errors.** Throw `AppError` (`AppError.notFound(...)`, `conflict`, `forbidden`...). Do not
  catch errors in controllers to send responses; the error handler does that. Responses for
  errors are always `{ error: { code, message, details? } }`.
- **Auth.** Routes are authenticated unless they are deliberately public (health). Use
  `requireRole('<AppRole>')` for role checks; app roles come from Entra ID.
- **Configuration.** Read settings only through `env` (src/config/env.ts). A new variable goes in
  the schema there and in `.env.example` with a safe placeholder. Never commit secrets.
- **Database.** Change `prisma/schema.prisma`, then `npm run db:migrate -- --name <change>`, and
  commit the generated migration. Never edit a migration that has been merged. Tables and
  columns are snake_case (`@@map`/`@map`), models are PascalCase.
- **API docs (CI).** Register every new path in `<name>.openapi.ts`, then `npm run openapi` and
  commit `openapi.json`. The frontend generates its client from it.
- **Logging.** `logger.info({ id }, 'message')`. No `console.log` in `src` (lint). Never log
  tokens, secrets or personal data.
- **Formatting and lint (CI).** `npm run format` and `npm run lint` before committing.

## Roles

The app roles are **Admin** and **User**: Entra ID app roles on the API's registration, assigned
through Azure AD groups, and present in the token's `roles` claim. `authenticate` accepts any
signed-in user; `requireRole('Admin')` limits a route to admins.

When a ticket defines different or additional roles, change them in one go: the routes'
`requireRole(...)` calls, the personas in `features/support/auth.ts`, the scenarios that sign in
as them, and this section. Role names must match the app roles defined in Entra ID.

## Tests

- **Acceptance (BDD, CI).** Every ticket with acceptance criteria adds or updates a feature file
  named after the ticket key: `features/PROJ-12-create-order.feature`. Write one scenario per
  Given/When/Then criterion, in the ticket's words. Reuse the generic steps in
  `features/step_definitions/http.steps.ts` (sign in as a persona, send a request, check the
  status and fields, remember values); add resource steps in `<resource>.steps.ts` only when the
  generic ones do not read well. Each scenario starts from empty tables. New personas go in
  `features/support/auth.ts`.
- **Unit (CI).** Every service method gets Vitest tests with a fake repository
  (`*.service.test.ts`): the normal case and each error it throws. `npm test` enforces at least
  80% line coverage of `src/modules/**/*.service.ts` (75% of branches); a change that adds
  logic without tests fails it. BDD covers routes, controllers and repositories end to end;
  unit tests cover the rules and edge cases that would be slow to reach through the API.
- **Smoke data.** The web app's nightly smoke run starts this API with `npm run start:smoke`
  (migrations, `scripts/dev-seed.ts`, `scripts/smoke-seed.ts`, the CI-only test issuer) and opens
  every page against it. When a module adds a model, add a few deterministic rows (fixed ids) to
  `smoke-seed.ts`, enough for each screen and each state the design draws (a late order, an empty
  list, a blocked item). Never set `AUTH_TEST_ISSUER` outside that run; the API refuses to start
  with it in production.
- **Reference data.** A feature that needs configuration data before it does anything (rules,
  calendars, standards, lookup tables) adds a sample to `scripts/dev-seed.ts` in the same change.
  `npm run seed:dev` loads it into a dev database; production gets the real data through the app.
- **Dates in data.** Fixtures, seeds, adapters' sample data and test data build dates with
  `src/lib/relativeDates.ts` (`weekDay(0, 2)` is this Wednesday), never a fixed calendar day:
  pinned dates age until every record is overdue and the current week is empty. A unit test that
  needs a fixed day passes its own `now`.
- Run locally: `docker compose up -d db`, `cp .env.example .env`, then `npm test` and
  `npm run test:bdd`.

## Commands

| Command                                                       | What it does                                             |
| ------------------------------------------------------------- | -------------------------------------------------------- |
| `npm run dev`                                                 | API with reload on http://localhost:3000 (docs at /docs) |
| `npm test`                                                    | unit tests                                               |
| `npm run test:bdd`                                            | migrations, then the Gherkin suite                       |
| `npm run lint` / `npm run typecheck` / `npm run format:check` | static checks                                            |
| `npm run db:migrate -- --name <change>`                       | create and apply a migration                             |
| `npm run openapi`                                             | regenerate `openapi.json`                                |
| `npm run build` / `npm start`                                 | production build and run                                 |
| `npm run start:smoke`                                         | migrate, seed, serve with the CI-only test issuer        |
| `npm run seed:dev`                                            | load sample reference data into a dev database           |
| `npm run --silent token:test -- --roles Admin`                | print a test issuer token for a `start:smoke` API        |
