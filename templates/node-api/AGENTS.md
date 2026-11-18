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
  named after the ticket key: `features/JMT-12-create-order.feature`. Write one scenario per
  Given/When/Then criterion, in the ticket's words. Reuse the generic steps in
  `features/step_definitions/http.steps.ts` (sign in as a persona, send a request, check the
  status and fields, remember values); add resource steps in `<resource>.steps.ts` only when the
  generic ones do not read well. Each scenario starts from empty tables. New personas go in
  `features/support/auth.ts`.
- **Unit (CI).** Business rules in services get Vitest tests with a fake repository
  (`*.service.test.ts`), including the error cases.
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
