import { decodeJwt, importSPKI, jwtVerify, type JWTPayload, type KeyLike } from 'jose';

/**
 * The CI-only test issuer: lets the nightly smoke run sign in to a real API without Entra ID.
 *
 * The API accepts tokens from it only when `AUTH_TEST_ISSUER=true` and NODE_ENV is not
 * `production` (env.ts refuses to start otherwise). Its issuer and audience can never match an
 * Entra ID token, and it signs with an asymmetric key whose public half the API reads from
 * `AUTH_TEST_PUBLIC_KEY`, so holding the API's configuration is not enough to mint a token.
 * Its tokens carry the same claims as Entra ID ones (oid, name, preferred_username, roles), so
 * role checks run exactly as in production.
 */
export const TEST_ISSUER = 'https://test-issuer.invalid/ts-harness';
export const TEST_AUDIENCE = 'api://test-issuer';
export const TEST_ALG = 'ES256';

/** The issuer claim of a token, without verifying it (to pick the key that verifies it). */
export function issuerOf(token: string): string | undefined {
  try {
    return decodeJwt(token).iss;
  } catch {
    return undefined;
  }
}

export async function loadTestIssuerKey(publicKeyPem: string): Promise<KeyLike> {
  return importSPKI(publicKeyPem.replace(/\\n/g, '\n'), TEST_ALG);
}

export async function verifyTestToken(token: string, key: KeyLike): Promise<JWTPayload> {
  const { payload } = await jwtVerify(token, key, {
    issuer: TEST_ISSUER,
    audience: TEST_AUDIENCE,
    algorithms: [TEST_ALG],
  });
  return payload;
}
