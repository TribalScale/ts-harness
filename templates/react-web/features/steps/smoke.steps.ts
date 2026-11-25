import { expect } from '@playwright/test';
import { Then, When } from '../support/fixtures';
import { failWith, type SmokeProblem } from '../support/smoke';

/**
 * Steps for the nightly smoke run (features tagged @smoke), against the real API. To run it
 * locally: start the API with `npm run start:smoke` in its checkout, then here
 * `SMOKE_API_URL=http://127.0.0.1:3000 SMOKE_API_DIR=../api npm run test:smoke`.
 */

/** The pages to open: every static route in src/router.tsx, read from the running app. */
async function appRoutes(page: import('@playwright/test').Page): Promise<string[]> {
  await page.goto('/');
  const handle = await page.waitForFunction(
    () => (window as unknown as { __APP_ROUTES__?: string[] }).__APP_ROUTES__,
  );
  return (await handle.jsonValue()) as string[];
}

Then('every page opens without errors', async ({ page, realApi, $testInfo }) => {
  const routes = await appRoutes(page);
  expect(routes.length, 'the app lists no routes (src/router.tsx)').toBeGreaterThan(0);
  const problems: SmokeProblem[] = [];
  for (const route of routes) {
    await page.goto(route);
    await page.waitForLoadState('networkidle');
    // RouteError's heading: the page threw while rendering (or loading its data).
    if (await page.getByRole('heading', { name: 'Something went wrong' }).isVisible()) {
      problems.push({
        route,
        kind: 'render_error',
        message: 'the page showed "Something went wrong"',
      });
    }
    const alert = page.getByRole('alert').first();
    if (await alert.isVisible()) {
      problems.push({ route, kind: 'render_error', message: `alert: ${await alert.innerText()}` });
    }
    if (realApi) problems.push(...(await realApi.take()));
  }
  if (problems.length) {
    const failed = new Set(problems.map((p) => p.route)).size;
    await failWith($testInfo, `${failed} of ${routes.length} pages failed`, problems);
  }
});

When(
  'I fill {string} with a unique {string}',
  async ({ page, unique }, label: string, prefix: string) => {
    unique[prefix] = `${prefix} ${Date.now().toString(36)}`;
    await page.getByLabel(label, { exact: true }).fill(unique[prefix]);
  },
);

Then('I see the heading with the unique {string}', async ({ page, unique }, prefix: string) => {
  const value = unique[prefix];
  if (!value) throw new Error(`No unique "${prefix}" was filled in earlier in this scenario`);
  await expect(page.getByRole('heading', { name: value })).toBeVisible();
});

Then('no API call failed', async ({ realApi, $testInfo }) => {
  const problems = realApi ? await realApi.take() : [];
  if (problems.length) await failWith($testInfo, 'API calls failed', problems);
});
