import type { NextFunction, Request, Response } from 'express';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import {
  generateTestIssuerKeys,
  mintTestToken,
  type TestIssuerKeys,
} from '../../scripts/test-issuer.js';

/**
 * The CI-only test issuer: the API trusts it only when asked to, never in production, and
 * only for tokens signed with its key. Each case loads env.ts and auth.ts afresh with its own
 * environment, as a server starting with that configuration would.
 */
let keys: TestIssuerKeys;
let otherKeys: TestIssuerKeys;
const admin = { id: 'admin-1', name: 'Test Admin', roles: ['Admin', 'User'] };

beforeAll(async () => {
  keys = await generateTestIssuerKeys();
  otherKeys = await generateTestIssuerKeys();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function loadAuth(env: Record<string, string>) {
  for (const [name, value] of Object.entries(env)) vi.stubEnv(name, value);
  vi.resetModules();
  return import('./auth.js');
}

/** Run `authenticate` with a bearer token: the principal it sets, or the error it throws. */
async function signIn(auth: Awaited<ReturnType<typeof loadAuth>>, token: string) {
  const req = { headers: { authorization: `Bearer ${token}` } } as Request;
  const next = vi.fn() as NextFunction;
  try {
    await auth.authenticate(req, {} as Response, next);
    return { principal: req.principal };
  } catch (error) {
    return { error: error as { status?: number } };
  }
}

describe('CI-only test issuer', () => {
  it('accepts its tokens when AUTH_TEST_ISSUER=true, with the same principal as Entra ID', async () => {
    const auth = await loadAuth({
      NODE_ENV: 'development',
      AUTH_TEST_ISSUER: 'true',
      AUTH_TEST_PUBLIC_KEY: keys.publicKey,
    });
    const result = await signIn(auth, await mintTestToken(keys.privateKey, admin));
    expect(result.principal).toEqual({
      id: 'admin-1',
      name: 'Test Admin',
      email: 'admin-1@example.com',
      roles: ['Admin', 'User'],
    });
  });

  it('maps roles so requireRole checks them', async () => {
    const auth = await loadAuth({
      NODE_ENV: 'development',
      AUTH_TEST_ISSUER: 'true',
      AUTH_TEST_PUBLIC_KEY: keys.publicKey,
    });
    const user = { id: 'user-1', name: 'Test User', roles: ['User'] };
    const { principal } = await signIn(auth, await mintTestToken(keys.privateKey, user));
    const req = { principal } as Request;
    expect(() => auth.requireRole('Admin')(req, {} as Response, vi.fn())).toThrow();
  });

  it('refuses its tokens without the flag', async () => {
    const auth = await loadAuth({ NODE_ENV: 'development', AUTH_TEST_PUBLIC_KEY: keys.publicKey });
    const result = await signIn(auth, await mintTestToken(keys.privateKey, admin));
    expect(result.error?.status).toBe(401);
  });

  it('refuses a token signed with another key', async () => {
    const auth = await loadAuth({
      NODE_ENV: 'development',
      AUTH_TEST_ISSUER: 'true',
      AUTH_TEST_PUBLIC_KEY: keys.publicKey,
    });
    const result = await signIn(auth, await mintTestToken(otherKeys.privateKey, admin));
    expect(result.error?.status).toBe(401);
  });

  it('refuses to start in production with the flag set', async () => {
    await expect(
      loadAuth({
        NODE_ENV: 'production',
        AUTH_TEST_ISSUER: 'true',
        AUTH_TEST_PUBLIC_KEY: keys.publicKey,
      }),
    ).rejects.toThrow(/AUTH_TEST_ISSUER=true is never allowed when NODE_ENV=production/);
  });

  it('refuses to start with the flag but no public key', async () => {
    await expect(loadAuth({ NODE_ENV: 'development', AUTH_TEST_ISSUER: 'true' })).rejects.toThrow(
      /AUTH_TEST_PUBLIC_KEY/,
    );
  });
});
