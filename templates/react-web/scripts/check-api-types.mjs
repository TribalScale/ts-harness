/* global console, process */
// Fail when API types are written by hand next to the generated ones. Once src/api/openapi.json
// exists, every API request and response type comes from src/api/schema.d.ts
// (`npm run api:generate`): hand-written types drift from the backend, and a fake API built from
// them passes every test while the real one breaks.
import { readdir, readFile, stat } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const GENERATED = 'schema.d.ts';
const DECLARATION =
  /^[ \t]*(?:export\s+)?(?:declare\s+)?(?:interface\s+(\w+)|type\s+(\w+)\s*(?:<[^=]*>)?\s*=([^;]*))/gm;
const FROM_SCHEMA = /\b(?:components|paths|operations|webhooks)\s*\[/;

async function exists(path) {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

async function sourceFiles(dir) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await sourceFiles(path)));
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) found.push(path);
  }
  return found;
}

/** Type names a file declares by hand: every interface, and every alias not taken from the schema. */
export function handWrittenTypes(text) {
  const names = [];
  for (const match of text.matchAll(DECLARATION)) {
    const [, iface, alias, body = ''] = match;
    if (iface) names.push(iface);
    else if (!FROM_SCHEMA.test(body) || body.includes('{')) names.push(alias);
  }
  return names;
}

/** `[{ file, names }]` for each file under `<root>/src/api` that declares API types by hand. */
export async function findHandWrittenTypes(root = '.') {
  const apiDir = join(root, 'src', 'api');
  if (!(await exists(join(apiDir, 'openapi.json')))) return [];
  const offenders = [];
  for (const path of await sourceFiles(apiDir)) {
    if (relative(apiDir, path) === GENERATED) continue;
    const names = handWrittenTypes(await readFile(path, 'utf8'));
    if (names.length) offenders.push({ file: relative(root, path), names });
  }
  return offenders.sort((a, b) => a.file.localeCompare(b.file));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const offenders = await findHandWrittenTypes(process.argv[2] ?? '.');
  if (offenders.length) {
    console.error(
      'API types must come from src/api/schema.d.ts, generated from src/api/openapi.json.',
    );
    console.error('These files declare API types by hand:');
    for (const { file, names } of offenders) console.error(`  ${file}: ${names.join(', ')}`);
    console.error(
      "Delete them and use components['schemas'][...] or paths[...] from './schema' instead.\n" +
        'Refresh the spec and types with `npm run api:pull && npm run api:generate`.',
    );
    process.exit(1);
  }
  console.log('API types: all from src/api/schema.d.ts');
}
