// Loaded before the app: the suite runs against the database in DATABASE_URL (local Postgres,
// the CI service, or the one a TS Harness coding job starts) with test tokens.
process.env.NODE_ENV = 'test';
process.env.DATABASE_URL ??= 'postgresql://app:app@localhost:5432/app';
process.env.AUTH_TENANT_ID ??= 'test-tenant';
process.env.AUTH_AUDIENCE ??= 'api://test';
process.env.AUTH_TEST_SECRET ??= 'test-secret-for-bdd-suite-only-0123456789';
