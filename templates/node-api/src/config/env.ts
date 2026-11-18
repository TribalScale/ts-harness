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
  })
  .superRefine((value, ctx) => {
    if (value.AUTH_TEST_SECRET && value.NODE_ENV !== 'test') {
      ctx.addIssue({
        code: 'custom',
        path: ['AUTH_TEST_SECRET'],
        message: 'AUTH_TEST_SECRET is only allowed when NODE_ENV=test',
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
