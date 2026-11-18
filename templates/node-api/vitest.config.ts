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
  },
});
