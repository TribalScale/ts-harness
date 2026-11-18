# Backend template

A starting point for TypeScript REST APIs: Express 5, Postgres (Prisma), Microsoft Entra ID
sign-in, Zod validation and OpenAPI docs, Cucumber acceptance tests, GitHub Actions CI and a
production Docker image.

## Quick start

```bash
nvm use                      # Node 22
npm install
cp .env.example .env
docker compose up -d db      # Postgres on localhost:5432
npm run db:deploy
npm run dev                  # http://localhost:3000, docs at /docs
```

Tests: `npm test` (unit) and `npm run test:bdd` (acceptance, needs the database).

## Start a new service from it

1. Create the repository from this template, or copy it in as the first commit.
2. Rename `name` in `package.json`; set the API title in `src/config/openapi.ts`.
3. Register the API in Entra ID (expose an API, define app roles). Set `AUTH_TENANT_ID` and
   `AUTH_AUDIENCE`.
4. Replace the `notes` reference module with your first resource once it exists (keep it until
   then as the example to copy).
5. Protect the base branch and require the CI jobs `checks`, `bdd` and `docker`.

See [AGENTS.md](AGENTS.md) for the conventions.

## Deploying

The image (`Dockerfile`) runs as a non-root user and exposes `/health` (liveness) and `/ready`
(database reachable). Run `npx prisma migrate deploy` with the same image as a release step
before starting new tasks. Supply configuration as environment variables; on AWS, inject
secrets from Secrets Manager (for example ECS task definition `secrets`).
