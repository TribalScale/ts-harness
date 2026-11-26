/* global fetch, console, process */
// Nightly smoke run: send TS Harness the design frame captures (frames-out/) for comparison with
// the design. Run by .github/workflows/nightly-smoke.yml after the smoke features.
//
//   node scripts/frames-report.mjs
//
// Commits frames-out/ to the branch ts-harness-evidence/design-conformance as one parentless
// commit (git plumbing: no checkout, the work branch is untouched), pushes it, then POSTs the
// commit to HARNESS_SMOKE_URL/frames with HARNESS_SMOKE_TOKEN. Never prints the token. Exits 0
// when there is nothing to send or the harness is not configured.
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';

const BRANCH = 'ts-harness-evidence/design-conformance';
const url = process.env.HARNESS_SMOKE_URL ?? '';
const token = process.env.HARNESS_SMOKE_TOKEN ?? '';
if (!url || !token) {
  console.log('frames: HARNESS_SMOKE_URL or HARNESS_SMOKE_TOKEN not set; nothing sent');
  process.exit(0);
}
if (!existsSync('frames-out/manifest.json')) {
  console.log('frames: no captures (the harness listed no frames)');
  process.exit(0);
}

const git = (args, input) => execFileSync('git', args, { input, encoding: 'utf8' }).trim();
const entries = readdirSync('frames-out')
  .filter((name) => name.endsWith('.png') || name === 'manifest.json')
  .map((name) => `100644 blob ${git(['hash-object', '-w', `frames-out/${name}`])}\t${name}`);
const tree = git(['mktree'], `${entries.join('\n')}\n`);
const commit = git([
  '-c',
  'user.name=TS Harness',
  '-c',
  'user.email=ts-harness@users.noreply.github.com',
  'commit-tree',
  tree,
  '-m',
  'Design frame captures',
]);
git(['push', '--force', 'origin', `${commit}:refs/heads/${BRANCH}`]);

const repo = process.env.GITHUB_REPOSITORY ?? '';
const runUrl = `${process.env.GITHUB_SERVER_URL}/${repo}/actions/runs/${process.env.GITHUB_RUN_ID}`;
const response = await fetch(`${url.replace(/\/$/, '')}/frames`, {
  method: 'POST',
  headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
  body: JSON.stringify({
    repo,
    run_url: runUrl,
    run_id: process.env.GITHUB_RUN_ID,
    branch: BRANCH,
    commit,
    web_sha: process.env.WEB_SHA ?? '',
  }),
});
console.log(`frames: ${entries.length - 1} captures sent (${response.status})`);
if (!response.ok) console.log(await response.text());
