import type { Page } from '@playwright/test';
import { test as base, createBdd } from 'playwright-bdd';
import { contractProblems } from './contract';

/** Who a scenario signs in as ("Given I am signed in as an admin"). */
export const PERSONAS: Record<string, { id: string; name: string; roles: string[] }> = {
  user: { id: 'user-1', name: 'Test User', roles: ['User'] },
  admin: { id: 'admin-1', name: 'Test Admin', roles: ['Admin'] },
};

type Row = Record<string, unknown> & { id: string };

/**
 * An in-memory API behind http://api.test, so acceptance tests run without the backend.
 * Seed it with `fakeApi.seed('notes', [...])`; requests the app makes read and change it.
 * Extend `handle` when a feature needs more endpoints, keeping the backend's contract
 * (openapi.json): same paths, bodies and `{ error: { code, message } }` errors.
 */
export class FakeApi {
  tables: Record<string, Row[]> = {};
  /** Requests or responses the backend's openapi.json would not allow (see contract.ts). */
  contractViolations: string[] = [];

  seed(table: string, rows: Record<string, unknown>[]) {
    this.tables[table] = rows.map((row) => ({
      id: crypto.randomUUID(),
      // The same defaults as a row the fake creates (POST below), so seeded rows match the contract.
      createdBy: 'user-1',
      body: '',
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      ...row,
    }));
  }

  async attach(page: Page) {
    await page.route('http://api.test/**', async (raw) => {
      const request = raw.request();
      const url = new URL(request.url());
      // Every reply is checked against the backend's contract before the app sees it.
      const route = {
        fulfill: (reply: { status?: number; json?: unknown }) => {
          let sent: unknown;
          try {
            sent = request.postDataJSON();
          } catch {
            sent = undefined;
          }
          this.contractViolations.push(
            ...contractProblems(
              request.method(),
              url.pathname,
              sent ?? undefined,
              reply.status ?? 200,
              reply.json,
            ),
          );
          return raw.fulfill(reply);
        },
      };
      const [table, id] = url.pathname.split('/').filter(Boolean);
      if (!table) return route.fulfill({ status: 404 });
      const rows = (this.tables[table] ??= []);
      const method = request.method();

      if (method === 'GET' && !id) {
        const search = url.searchParams.get('search')?.toLowerCase();
        const data = search
          ? rows.filter((r) =>
              String(r.title ?? '')
                .toLowerCase()
                .includes(search),
            )
          : rows;
        return route.fulfill({ json: { data, page: 1, limit: 50, total: data.length } });
      }
      if (method === 'POST' && !id) {
        const body = request.postDataJSON() as Record<string, unknown>;
        if (rows.some((r) => r.title === body.title)) {
          return route.fulfill({
            status: 409,
            json: {
              error: {
                code: 'conflict',
                message: `A ${table.replace(/s$/, '')} with this title already exists`,
              },
            },
          });
        }
        const row = {
          id: crypto.randomUUID(),
          createdBy: 'user-1',
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
          body: '',
          ...body,
        };
        rows.push(row);
        return route.fulfill({ status: 201, json: row });
      }
      const row = rows.find((r) => r.id === id);
      if (!row)
        return route.fulfill({
          status: 404,
          json: { error: { code: 'not_found', message: 'Not found' } },
        });
      if (method === 'GET') return route.fulfill({ json: row });
      if (method === 'DELETE') {
        rows.splice(rows.indexOf(row), 1);
        return route.fulfill({ status: 204 });
      }
      return route.fulfill({ status: 405 });
    });
  }
}

export const test = base.extend<{ fakeApi: FakeApi }>({
  // auto: every scenario gets the fake API, also those that never seed data.
  fakeApi: [
    async ({ page }, use) => {
      const api = new FakeApi();
      await api.attach(page);
      await use(api);
      if (api.contractViolations.length) {
        throw new Error(
          "The fake API broke the backend's contract (openapi.json):\n- " +
            [...new Set(api.contractViolations)].join('\n- '),
        );
      }
    },
    { auto: true },
  ],
});

export const { Given, When, Then } = createBdd(test);
