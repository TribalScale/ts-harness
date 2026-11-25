import { z } from 'zod';

/**
 * Every environment variable the app reads, validated once at startup. Add new variables here
 * and to .env.example; read them through `env`, never through process.env elsewhere.
 */
const schema = z
  .object({
    NODE_ENV: z.enum(['development', 'test', 'production']).default('development'),
    PORT: z.coerce.number().int().positive().default(3000),
    LOG_LEVEL: z
      .enum(['fatal', 'error', 'warn', 'info', 'debug', 'trace', 'silent'])
      .default('info'),
    DATABASE_URL: z.string().url(),
    CORS_ORIGINS: z
      .string()
      .default('')
      .transform((value) =>
        value
          .split(',')
          .map((origin) => origin.trim())
          .filter(Boolean),
      ),
    AUTH_TENANT_ID: z.string().min(1),
    AUTH_AUDIENCE: z.string().min(1),
    AUTH_TEST_SECRET: z.string().optional(),
    // CI-only test issuer (nightly smoke run against a real database): see testIssuer.ts.
    AUTH_TEST_ISSUER: z
      .enum(['true', 'false', ''])
      .default('false')
      .transform((value) => value === 'true'),
    AUTH_TEST_PUBLIC_KEY: z.string().optional(),
  })
  .superRefine((value, ctx) => {
    if (value.AUTH_TEST_SECRET && value.NODE_ENV !== 'test') {
      ctx.addIssue({
        code: 'custom',
        path: ['AUTH_TEST_SECRET'],
        message: 'AUTH_TEST_SECRET is only allowed when NODE_ENV=test',
      });
    }
    if (value.AUTH_TEST_ISSUER && value.NODE_ENV === 'production') {
      // A production API that trusts a key anyone with CI access can hold is a backdoor.
      ctx.addIssue({
        code: 'custom',
        path: ['AUTH_TEST_ISSUER'],
        message:
          'AUTH_TEST_ISSUER=true is never allowed when NODE_ENV=production; refusing to start',
      });
    }
    if (value.AUTH_TEST_ISSUER && !value.AUTH_TEST_PUBLIC_KEY) {
      ctx.addIssue({
        code: 'custom',
        path: ['AUTH_TEST_PUBLIC_KEY'],
        message: 'AUTH_TEST_ISSUER=true needs AUTH_TEST_PUBLIC_KEY (npm run start:smoke sets it)',
      });
    }
  });

export type Env = z.infer<typeof schema>;

function load(): Env {
  const parsed = schema.safeParse(process.env);
  if (!parsed.success) {
    const problems = parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ');
    throw new Error(`Invalid environment: ${problems}`);
  }
  return parsed.data;
}

export const env = load();
