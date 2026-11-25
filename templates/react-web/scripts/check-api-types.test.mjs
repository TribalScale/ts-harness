import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { findHandWrittenTypes, handWrittenTypes } from './check-api-types.mjs';

const script = join(import.meta.dirname, 'check-api-types.mjs');
const roots = [];

function project(files) {
  const root = mkdtempSync(join(tmpdir(), 'check-api-types-'));
  roots.push(root);
  for (const [path, text] of Object.entries(files)) {
    mkdirSync(join(root, path, '..'), { recursive: true });
    writeFileSync(join(root, path), text);
  }
  return root;
}

function run(root) {
  try {
    return {
      code: 0,
      output: execFileSync('node', [script, root], { encoding: 'utf8', stdio: 'pipe' }),
    };
  } catch (error) {
    return { code: error.status, output: String(error.stderr) };
  }
}

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe('check-api-types', () => {
  it('names interfaces and aliases that are not taken from the schema', () => {
    const text = [
      'export interface Note { id: string }',
      "export type Status = 'open' | 'closed';",
      "export type Page = components['schemas']['NotePage'];",
      "type Created =\n  paths['/notes']['post']['responses'][201]['content']['application/json'];",
      "export type Mixed = { note: components['schemas']['Note']; extra: string };",
    ].join('\n');
    expect(handWrittenTypes(text)).toEqual(['Note', 'Status', 'Mixed']);
  });

  it('fails on a hand-written API type file and names it', async () => {
    const root = project({
      'src/api/openapi.json': '{}',
      'src/api/schema.d.ts': 'export interface paths {}',
      'src/api/client.ts': "import type { paths } from './schema';\nexport const x = 1;",
      'src/api/types.ts': 'export interface Note { id: string; title: string }',
      'src/api/client.test.ts': 'interface Fixture { a: number }',
    });
    expect(await findHandWrittenTypes(root)).toEqual([
      { file: 'src/api/types.ts', names: ['Note'] },
    ]);
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.output).toContain('src/api/types.ts: Note');
  });

  it('passes on the template and when there is no spec yet', async () => {
    expect(run(join(import.meta.dirname, '..')).code).toBe(0);
    const noSpec = project({ 'src/api/types.ts': 'export interface Note { id: string }' });
    expect(await findHandWrittenTypes(noSpec)).toEqual([]);
  });
});
