import type { Page, Response, TestInfo } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { contractProblems } from './contract';

/**
 * The nightly smoke run: the built app against the real API (started with the API repo's
 * `npm run start:smoke`) and a real database, instead of the fake API. It catches what the fake
 * cannot: the app's types drifting from what the API really sends, a page that crashes on real
 * data, a call the real API refuses.
 *
 * On when SMOKE_API_URL is set (the API's address); playwright.config.ts then runs only the
 * features tagged @smoke. SMOKE_API_DIR is the API repository's checkout, whose
 * `npm run token:test` mints sign-in tokens from its CI-only test issuer.
 */
export const SMOKE_API_URL = process.env.SMOKE_API_URL ?? '';
export const smokeMode = SMOKE_API_URL !== '';

/** One thing that went wrong, with where: the failure report sent to TS Harness lists these. */
export interface SmokeProblem {
  route: string;
  kind: 'render_error' | 'failed_api_call' | 'contract_violation' | 'page_error';
  message: string;
}

/**
 * Watches every call the page makes to the real API: a failed call (4xx/5xx or no response)
 * and a request or response the backend's openapi.json does not allow are problems, as is an
 * uncaught error in the page. The same contract check as against the fake API, now on real
 * responses.
 */
export class RealApiMonitor {
  private problems: SmokeProblem[] = [];
  private pending: Promise<void>[] = [];
  private readonly base: URL;

  constructor(apiUrl: string) {
    this.base = new URL(apiUrl);
  }

  attach(page: Page) {
    const routeOf = () => {
      try {
        return new URL(page.url()).pathname;
      } catch {
        return '';
      }
    };
    page.on('response', (response) => {
      if (this.isApi(response.url())) this.pending.push(this.check(response, routeOf()));
    });
    page.on('requestfailed', (request) => {
      if (!this.isApi(request.url()) || request.method() === 'OPTIONS') return;
      this.problems.push({
        route: routeOf(),
        kind: 'failed_api_call',
        message: `${request.method()} ${this.apiPath(request.url())}: no response (${request.failure()?.errorText ?? 'failed'})`,
      });
    });
    page.on('pageerror', (error) => {
      this.problems.push({ route: routeOf(), kind: 'page_error', message: error.message });
    });
  }

  add(problem: SmokeProblem) {
    this.problems.push(problem);
  }

  /** Wait for the responses still being checked, then hand over (and forget) the problems. */
  async take(): Promise<SmokeProblem[]> {
    await Promise.all(this.pending.splice(0));
    return this.problems.splice(0);
  }

  private isApi(url: string) {
    return url.startsWith(this.base.origin) && new URL(url).pathname.startsWith(this.prefix());
  }

  private prefix() {
    return this.base.pathname.replace(/\/$/, '');
  }

  private apiPath(url: string) {
    return new URL(url).pathname.slice(this.prefix().length) || '/';
  }

  private async check(response: Response, route: string) {
    const request = response.request();
    const method = request.method();
    if (method === 'OPTIONS') return;
    const path = this.apiPath(response.url());
    const status = response.status();
    let body: unknown;
    try {
      body = status === 204 ? undefined : await response.json();
    } catch {
      body = undefined;
    }
    let sent: unknown;
    try {
      sent = request.postDataJSON() ?? undefined;
    } catch {
      sent = undefined;
    }
    if (status >= 400) {
      this.problems.push({
        route,
        kind: 'failed_api_call',
        message: `${method} ${path} returned ${status}: ${JSON.stringify(body ?? null).slice(0, 300)}`,
      });
    }
    for (const message of contractProblems(method, path, sent, status, body)) {
      this.problems.push({ route, kind: 'contract_violation', message });
    }
  }
}

/** The same problem seen twice (a query refetching) is listed once. */
export function uniqueProblems(problems: SmokeProblem[]): SmokeProblem[] {
  const seen = new Set<string>();
  return problems.filter((p) => {
    const key = `${p.route} ${p.kind} ${p.message}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function describeProblems(problems: SmokeProblem[]): string {
  return uniqueProblems(problems)
    .map((p) => `[${p.route || '?'}] ${p.kind}: ${p.message}`)
    .join('\n- ');
}

/**
 * Fail the scenario with these problems, attached as `smoke-problems` (JSON) so the failure
 * report sent to TS Harness (scripts/smoke-report.mjs) knows each one's route and kind.
 */
export async function failWith(
  testInfo: TestInfo,
  heading: string,
  problems: SmokeProblem[],
): Promise<never> {
  const list = uniqueProblems(problems);
  await testInfo.attach('smoke-problems', {
    body: JSON.stringify(list),
    contentType: 'application/json',
  });
  throw new Error(`${heading}:\n- ${describeProblems(list)}`);
}

const tokens = new Map<string, string>();

/** A token from the API's CI-only test issuer for this persona (minted once per worker). */
export function smokeToken(user: { id: string; name: string; roles: string[] }): string {
  const key = `${user.id}:${user.roles.join(',')}`;
  const cached = tokens.get(key);
  if (cached) return cached;
  const apiDir = process.env.SMOKE_API_DIR;
  if (!apiDir) throw new Error('SMOKE_API_DIR (the API checkout) is required in the smoke run');
  const token = execFileSync(
    'npm',
    [
      'run',
      '--silent',
      'token:test',
      '--',
      '--roles',
      user.roles.join(','),
      '--id',
      user.id,
      '--name',
      user.name,
    ],
    { cwd: apiDir, encoding: 'utf8' },
  ).trim();
  tokens.set(key, token);
  return token;
}
