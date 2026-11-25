import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { exportPKCS8, exportSPKI, generateKeyPair, importPKCS8, SignJWT } from 'jose';
import { TEST_ALG, TEST_AUDIENCE, TEST_ISSUER } from '../src/middlewares/testIssuer.js';

/**
 * The CI-only test issuer's signing side (see src/middlewares/testIssuer.ts): its key pair and
 * the tokens it mints. Never imported by the server: only `start:smoke`, `token:test` and tests.
 */

/** Where `start:smoke` keeps a generated key pair when none comes from the environment. */
export const KEY_DIR = '.smoke';
const PRIVATE_FILE = join(KEY_DIR, 'test-issuer.key.pem');
const PUBLIC_FILE = join(KEY_DIR, 'test-issuer.pub.pem');

export interface TestIssuerKeys {
  privateKey: string;
  publicKey: string;
}

export async function generateTestIssuerKeys(): Promise<TestIssuerKeys> {
  const { privateKey, publicKey } = await generateKeyPair(TEST_ALG, { extractable: true });
  return { privateKey: await exportPKCS8(privateKey), publicKey: await exportSPKI(publicKey) };
}

/**
 * The key pair from AUTH_TEST_PRIVATE_KEY / AUTH_TEST_PUBLIC_KEY, else from .smoke/, generating
 * it there on first use (so `token:test` signs with the key `start:smoke` trusts).
 */
export async function ensureTestIssuerKeys(): Promise<TestIssuerKeys> {
  const fromEnv = process.env.AUTH_TEST_PRIVATE_KEY;
  if (fromEnv && process.env.AUTH_TEST_PUBLIC_KEY) {
    return { privateKey: fromEnv, publicKey: process.env.AUTH_TEST_PUBLIC_KEY };
  }
  if (existsSync(PRIVATE_FILE) && existsSync(PUBLIC_FILE)) {
    return {
      privateKey: readFileSync(PRIVATE_FILE, 'utf8'),
      publicKey: readFileSync(PUBLIC_FILE, 'utf8'),
    };
  }
  const keys = await generateTestIssuerKeys();
  mkdirSync(dirname(PRIVATE_FILE), { recursive: true });
  writeFileSync(PRIVATE_FILE, keys.privateKey, { mode: 0o600 });
  writeFileSync(PUBLIC_FILE, keys.publicKey);
  return keys;
}

export interface TestUser {
  id: string;
  name: string;
  roles: string[];
  email?: string;
}

/** A token shaped like an Entra ID access token (oid, name, preferred_username, roles). */
export async function mintTestToken(
  privateKeyPem: string,
  user: TestUser,
  expiresIn = '2h',
): Promise<string> {
  const key = await importPKCS8(privateKeyPem.replace(/\\n/g, '\n'), TEST_ALG);
  return new SignJWT({
    oid: user.id,
    name: user.name,
    preferred_username: user.email ?? `${user.id}@example.com`,
    roles: user.roles,
  })
    .setProtectedHeader({ alg: TEST_ALG })
    .setIssuer(TEST_ISSUER)
    .setAudience(TEST_AUDIENCE)
    .setSubject(user.id)
    .setIssuedAt()
    .setExpirationTime(expiresIn)
    .sign(key);
}
