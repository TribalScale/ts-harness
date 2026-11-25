import { z } from 'zod';

/**
 * Every VITE_* setting the app reads, validated at startup. Values are baked in at build time
 * and visible to anyone using the app: never put secrets here.
 */
const schema = z.discriminatedUnion('VITE_AUTH_MODE', [
  z.object({
    VITE_AUTH_MODE: z.literal('msal'),
    VITE_API_URL: z.string().url(),
    VITE_AUTH_CLIENT_ID: z.string().min(1),
    VITE_AUTH_TENANT_ID: z.string().min(1),
    VITE_API_SCOPE: z.string().min(1),
  }),
  z.object({
    // Acceptance and unit tests only: a fake signed-in user, no Microsoft sign-in.
    VITE_AUTH_MODE: z.literal('test'),
    VITE_API_URL: z.string().url(),
  }),
]);

const parsed = schema.safeParse(import.meta.env);
if (!parsed.success) {
  throw new Error(
    'Invalid configuration: ' +
      parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; '),
  );
}
if (
  parsed.data.VITE_AUTH_MODE === 'test' &&
  import.meta.env.PROD &&
  import.meta.env.MODE !== 'e2e' &&
  import.meta.env.MODE !== 'smoke'
) {
  throw new Error('VITE_AUTH_MODE=test is not allowed in a production build');
}

export const env = parsed.data;
