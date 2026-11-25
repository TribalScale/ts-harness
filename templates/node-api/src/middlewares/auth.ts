import type { RequestHandler } from 'express';
import { createRemoteJWKSet, jwtVerify, type JWTPayload } from 'jose';
import { env } from '../config/env.js';
import { AppError } from '../errors/appError.js';
import { issuerOf, loadTestIssuerKey, TEST_ISSUER, verifyTestToken } from './testIssuer.js';

/** The signed-in caller, from the access token. */
export interface Principal {
  id: string;
  email?: string;
  name?: string;
  roles: string[];
}

declare module 'express-serve-static-core' {
  interface Request {
    principal?: Principal;
  }
}

const issuer = `https://login.microsoftonline.com/${env.AUTH_TENANT_ID}/v2.0`;
const jwks = createRemoteJWKSet(
  new URL(`https://login.microsoftonline.com/${env.AUTH_TENANT_ID}/discovery/v2.0/keys`),
);
const testKey = env.AUTH_TEST_SECRET ? new TextEncoder().encode(env.AUTH_TEST_SECRET) : undefined;
// AUTH_TEST_ISSUER=true outside production only (enforced in env.ts): the nightly smoke run.
const testIssuerKey =
  env.AUTH_TEST_ISSUER && env.AUTH_TEST_PUBLIC_KEY
    ? loadTestIssuerKey(env.AUTH_TEST_PUBLIC_KEY)
    : undefined;

async function verify(token: string): Promise<JWTPayload> {
  if (testIssuerKey && issuerOf(token) === TEST_ISSUER) {
    return verifyTestToken(token, await testIssuerKey);
  }
  if (testKey) {
    // NODE_ENV=test only (enforced in env.ts): tests sign their own tokens.
    return (await jwtVerify(token, testKey, { issuer, audience: env.AUTH_AUDIENCE })).payload;
  }
  // Any other issuer is refused before fetching Entra ID's signing keys.
  if (issuerOf(token) !== issuer) throw new Error('unknown token issuer');
  return (await jwtVerify(token, jwks, { issuer, audience: env.AUTH_AUDIENCE })).payload;
}

function toPrincipal(payload: JWTPayload): Principal {
  const claims = payload as JWTPayload & Record<string, unknown>;
  return {
    id: String(claims.oid ?? claims.sub ?? ''),
    email: typeof claims.preferred_username === 'string' ? claims.preferred_username : undefined,
    name: typeof claims.name === 'string' ? claims.name : undefined,
    roles: Array.isArray(claims.roles) ? claims.roles.map(String) : [],
  };
}

/** Require a valid Entra ID access token (Authorization: Bearer ...). */
export const authenticate: RequestHandler = async (req, _res, next) => {
  const header = req.headers.authorization ?? '';
  const [scheme, token] = header.split(' ');
  if (scheme !== 'Bearer' || !token) throw AppError.unauthorized();
  try {
    req.principal = toPrincipal(await verify(token));
  } catch {
    throw AppError.unauthorized('Invalid or expired token');
  }
  next();
};

/**
 * Require one of the app roles (Entra ID app roles, mapped from Azure AD groups). Use after
 * `authenticate`.
 */
export function requireRole(...roles: string[]): RequestHandler {
  return (req, _res, next) => {
    const have = req.principal?.roles ?? [];
    if (!roles.some((role) => have.includes(role))) throw AppError.forbidden();
    next();
  };
}
