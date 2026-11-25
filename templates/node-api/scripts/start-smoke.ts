import { execFileSync } from 'node:child_process';
import { ensureTestIssuerKeys } from './test-issuer.js';

/**
 * `npm run start:smoke`: the API as the nightly smoke run uses it. Applies migrations, seeds
 * deterministic data (scripts/smoke-seed.ts), then serves with the CI-only test issuer on, so
 * the web app's smoke features can sign in without Entra ID. Fixture adapters only: anything
 * that would reach an external system (S3, queues, SAP...) gets a fixture in `createDeps` below.
 *
 * Refuses NODE_ENV=production. Needs DATABASE_URL (a throwaway Postgres).
 */
if (process.env.NODE_ENV === 'production') {
  console.error('start:smoke refuses NODE_ENV=production: it turns on the CI-only test issuer.');
  process.exit(1);
}

const keys = await ensureTestIssuerKeys();
process.env.AUTH_TEST_ISSUER = 'true';
process.env.AUTH_TEST_PUBLIC_KEY = keys.publicKey;
// The real issuer is never used in a smoke run; placeholders let the configuration load.
process.env.AUTH_TENANT_ID ||= 'smoke-tenant';
process.env.AUTH_AUDIENCE ||= 'api://smoke';
process.env.CORS_ORIGINS ||= 'http://127.0.0.1:4173,http://localhost:4173';

execFileSync('npx', ['prisma', 'migrate', 'deploy'], { stdio: 'inherit' });

const { env } = await import('../src/config/env.js');
const { prisma } = await import('../src/db/prisma.js');
const { seedSmoke } = await import('./smoke-seed.js');
await seedSmoke(prisma);

const { createApp } = await import('../src/app.js');
const { createDeps } = await import('../src/container.js');
// Fixture adapters go here once the API has external clients: createDeps({ s3: fakeS3 }).
createApp(createDeps()).listen(env.PORT, () => {
  console.log(`smoke API listening on port ${env.PORT} (test issuer on, seeded)`);
});
