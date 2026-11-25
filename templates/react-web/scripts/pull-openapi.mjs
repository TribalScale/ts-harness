/* global fetch, console, process */
// Copy the backend's openapi.json (a path or a URL) to src/api/openapi.json: the typed client
// is generated from it, and the acceptance tests check the fake API against it.
import { readFile, writeFile } from 'node:fs/promises';

const source = process.env.OPENAPI_SOURCE || '../backend/openapi.json';
const text = /^https?:\/\//.test(source)
  ? await (await fetch(source)).text()
  : await readFile(source, 'utf8');
JSON.parse(text); // fail early on anything that is not JSON
await writeFile('src/api/openapi.json', text);
console.log(`src/api/openapi.json <- ${source}`);
