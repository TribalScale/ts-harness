import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    env: {
      NODE_ENV: 'test',
      DATABASE_URL: 'postgresql://unit:unit@localhost:5432/unit',
      AUTH_TENANT_ID: 'test-tenant',
      AUTH_AUDIENCE: 'api://test',
    },
    coverage: {
      provider: 'v8',
      // Business rules live in services; unit tests must cover them (the BDD suite covers
      // routes, controllers and repositories end to end).
      include: ['src/modules/**/*.service.ts'],
      reporter: ['text-summary', 'text'],
      thresholds: { lines: 80, functions: 80, branches: 75, statements: 80 },
    },
  },
});
