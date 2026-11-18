import { SignJWT } from 'jose';

/** A signed test access token shaped like an Entra ID one (oid, name, roles). */
export async function testToken(opts: {
  id: string;
  name: string;
  roles: string[];
}): Promise<string> {
  const secret = new TextEncoder().encode(process.env.AUTH_TEST_SECRET);
  return new SignJWT({
    oid: opts.id,
    name: opts.name,
    preferred_username: `${opts.id}@example.com`,
    roles: opts.roles,
  })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuer(`https://login.microsoftonline.com/${process.env.AUTH_TENANT_ID}/v2.0`)
    .setAudience(process.env.AUTH_AUDIENCE!)
    .setIssuedAt()
    .setExpirationTime('10m')
    .sign(secret);
}

/** Roles a scenario can sign in as ("Given I am signed in as an admin"). */
export const PERSONAS: Record<string, string[]> = {
  admin: ['Admin'],
  user: ['User'],
};
