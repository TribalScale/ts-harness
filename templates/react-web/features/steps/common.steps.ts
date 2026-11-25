import { expect } from '@playwright/test';
import { Given, PERSONAS, Then, When } from '../support/fixtures';
import { smokeMode, smokeToken } from '../support/smoke';

/**
 * Generic UI steps every feature can use. They find elements the way people do (role, label,
 * visible text), so they also keep the UI accessible. Add feature-specific steps in their own
 * file (features/steps/<feature>.steps.ts) only when these do not read well.
 */
Given('I am signed in as a/an {word}', async ({ page }, persona: string) => {
  const user = PERSONAS[persona];
  if (!user)
    throw new Error(`Unknown persona "${persona}". Add it to features/support/fixtures.ts`);
  // The smoke run signs in for real: a token the real API accepts, from its test issuer.
  const token = smokeMode ? smokeToken(user) : null;
  await page.addInitScript(
    ([value, realToken]) => {
      localStorage.setItem('test-auth-user', value);
      if (realToken) localStorage.setItem('test-auth-token', realToken);
    },
    [JSON.stringify(user), token] as const,
  );
});

Given('I am not signed in', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('test-auth-user', 'signed-out'));
});

Given(
  'the API has {word}:',
  async ({ fakeApi }, table: string, rows: { hashes(): Record<string, string>[] }) => {
    if (smokeMode) {
      throw new Error('The smoke run uses the real API: seed data in its scripts/smoke-seed.ts');
    }
    fakeApi.seed(table, rows.hashes());
  },
);

When('I open {string}', async ({ page }, path: string) => {
  await page.goto(path);
});

When('I fill {string} with {string}', async ({ page }, label: string, value: string) => {
  await page.getByLabel(label, { exact: true }).fill(value);
});

When('I click {string}', async ({ page }, name: string) => {
  await page.getByRole('button', { name }).click();
});

Then('I see {string}', async ({ page }, text: string) => {
  await expect(page.getByText(text, { exact: true }).first()).toBeVisible();
});

Then('I do not see {string}', async ({ page }, text: string) => {
  await expect(page.getByText(text, { exact: true })).toHaveCount(0);
});

Then('I see the heading {string}', async ({ page }, name: string) => {
  await expect(page.getByRole('heading', { name })).toBeVisible();
});
