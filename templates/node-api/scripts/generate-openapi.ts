import { writeFileSync } from 'node:fs';

// Documentation needs no real configuration; placeholders let the modules load.
process.env.DATABASE_URL ??= 'postgresql://openapi:openapi@localhost:5432/openapi';
process.env.AUTH_TENANT_ID ??= 'openapi';
process.env.AUTH_AUDIENCE ??= 'openapi';

await import('../src/modules/index.js');
const { openApiDocument } = await import('../src/config/openapi.js');

// The frontend generates its typed client from this file. CI fails when it is out of date.
writeFileSync('openapi.json', JSON.stringify(openApiDocument(), null, 2) + '\n');
console.log('wrote openapi.json');
