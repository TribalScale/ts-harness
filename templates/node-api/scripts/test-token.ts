import { parseArgs } from 'node:util';
import { ensureTestIssuerKeys, mintTestToken } from './test-issuer.js';

/**
 * Print a CI-only test issuer token for an API started with `npm run start:smoke`:
 *
 *   npm run --silent token:test -- --roles Admin,User --id admin-1 --name "Test Admin"
 *
 * The API accepts it only while AUTH_TEST_ISSUER=true outside production.
 */
const { values } = parseArgs({
  options: {
    roles: { type: 'string', default: 'User' },
    id: { type: 'string', default: 'smoke-user' },
    name: { type: 'string', default: 'Smoke Test User' },
    email: { type: 'string' },
    expires: { type: 'string', default: '2h' },
  },
});

const keys = await ensureTestIssuerKeys();
const roles = values.roles
  .split(',')
  .map((role) => role.trim())
  .filter(Boolean);
const token = await mintTestToken(
  keys.privateKey,
  { id: values.id, name: values.name, roles, email: values.email },
  values.expires,
);
process.stdout.write(token + '\n');
