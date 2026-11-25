/* global fetch, console, process, Buffer */
// Nightly smoke run, on failure: send TS Harness what failed, so it can propose one bug
// (a person approves it before anything reaches Jira). Run by .github/workflows/nightly-smoke.yml.
//
//   node scripts/smoke-report.mjs [smoke-results.json]
//
// Reads Playwright's JSON report and POSTs to HARNESS_SMOKE_URL with HARNESS_SMOKE_TOKEN:
// the repositories, the run, both commits, and each failing scenario with its step, route and
// error. With no report (the API or the build failed before any feature ran) it reports that.
// Never prints the token. Exits 0 when the harness is not configured: the run is red anyway.
import { existsSync, readFileSync } from 'node:fs';

const MAX_FAILURES = 50;
const MAX_ERROR = 2000;

/** Strip terminal colours and keep errors short enough to read in a bug report. */
function clean(text) {
  return (
    String(text ?? '')
      // eslint-disable-next-line no-control-regex
      .replace(/\u001b\[[0-9;]*m/g, '')
      .trim()
      .slice(0, MAX_ERROR)
  );
}

function* tests(suite, path = []) {
  const here = suite.title ? [...path, suite.title] : path;
  for (const spec of suite.specs ?? []) {
    for (const test of spec.tests ?? []) yield { spec, test, path: here };
  }
  for (const child of suite.suites ?? []) yield* tests(child, here);
}

function failedStep(result) {
  const step = (result.steps ?? []).find((s) => s.error);
  return step ? step.title : '';
}

function problemsOf(result) {
  const attachment = (result.attachments ?? []).find((a) => a.name === 'smoke-problems' && a.body);
  if (!attachment) return [];
  try {
    return JSON.parse(Buffer.from(attachment.body, 'base64').toString('utf8'));
  } catch {
    return [];
  }
}

/** One entry per problem (route, kind), or one per failing scenario when it has none. */
export function failuresFrom(report) {
  const failures = [];
  for (const suite of report.suites ?? []) {
    for (const { spec, test, path } of tests(suite)) {
      const results = test.results ?? [];
      const last = results[results.length - 1];
      if (!last || last.status === 'passed' || last.status === 'skipped') continue;
      const scenario = spec.title;
      const feature = path.filter((p) => !p.endsWith('.js')).pop() ?? '';
      const step = failedStep(last);
      const problems = problemsOf(last);
      if (problems.length) {
        for (const p of problems) {
          failures.push({
            feature,
            scenario,
            step,
            route: p.route ?? '',
            kind: p.kind ?? '',
            error: clean(p.message),
          });
        }
      } else {
        failures.push({
          feature,
          scenario,
          step,
          route: '',
          kind: 'scenario_failed',
          error: clean(last.error?.message ?? last.status),
        });
      }
    }
  }
  return failures.slice(0, MAX_FAILURES);
}

async function main() {
  const file = process.argv[2] ?? 'smoke-results.json';
  const env = process.env;
  const url = env.HARNESS_SMOKE_URL;
  if (!url || !env.HARNESS_SMOKE_TOKEN) {
    console.log('HARNESS_SMOKE_URL / HARNESS_SMOKE_TOKEN are not set: no failure report sent.');
    return;
  }
  let failures;
  if (existsSync(file)) {
    failures = failuresFrom(JSON.parse(readFileSync(file, 'utf8')));
  }
  if (!failures?.length) {
    failures = [
      {
        feature: '',
        scenario: 'Smoke run setup',
        step: env.SMOKE_FAILED_STEP ?? '',
        route: '',
        kind: 'setup_failed',
        error:
          'The smoke run failed before its features finished (API start, build or install). See the run log.',
      },
    ];
  }
  const server = env.GITHUB_SERVER_URL ?? 'https://github.com';
  const report = {
    repo: env.GITHUB_REPOSITORY,
    api_repo: env.API_REPO ?? '',
    api_ref: env.API_REF ?? '',
    run_id: env.GITHUB_RUN_ID ?? '',
    run_url: `${server}/${env.GITHUB_REPOSITORY}/actions/runs/${env.GITHUB_RUN_ID}`,
    web_sha: env.WEB_SHA ?? env.GITHUB_SHA ?? '',
    api_sha: env.API_SHA ?? '',
    artifact_url: env.ARTIFACT_URL ?? '',
    failures,
  };
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${env.HARNESS_SMOKE_TOKEN}`,
    },
    body: JSON.stringify(report),
  });
  const text = await response.text();
  console.log(`TS Harness answered ${response.status}: ${text.slice(0, 500)}`);
  if (!response.ok) process.exitCode = 1;
}

if (import.meta.url === `file://${process.argv[1]}`) await main();
