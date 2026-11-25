import { defineConfig, devices } from '@playwright/test';
import { defineBddConfig } from 'playwright-bdd';

// SMOKE_API_URL set: the nightly smoke run. Only @smoke features, against the real API that
// address serves (see features/support/smoke.ts); otherwise every other feature, against the
// fake API.
const smoke = !!process.env.SMOKE_API_URL;

// Gherkin features in features/*.feature become Playwright tests (bddgen writes .features-gen).
const testDir = defineBddConfig({
  features: 'features/**/*.feature',
  steps: ['features/steps/**/*.ts', 'features/support/**/*.ts'],
  tags: smoke ? '@smoke' : 'not @smoke',
  outputDir: smoke ? '.features-gen/smoke' : '.features-gen/default',
});

export default defineConfig({
  testDir,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['line'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: smoke ? 'only-on-failure' : 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    // The smoke run serves a production build (vite build --mode smoke), as users get it.
    command: smoke ? 'npm run serve:smoke' : 'npm run dev:e2e',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: smoke ? 180_000 : 60_000,
  },
});
