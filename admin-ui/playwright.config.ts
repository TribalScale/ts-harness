import { defineConfig, devices } from "@playwright/test";

/**
 * Browser tests for the admin console. They run the built UI (`vite preview` serves `dist/`)
 * against a mocked gateway: every API call is answered by route interception in
 * `e2e/mockApi.ts`, so no backend is needed. `npm run test:e2e` builds first.
 */
const PORT = Number(process.env.E2E_UI_PORT || 4173);

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
    headless: true,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: {
    command: `npx vite preview --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
