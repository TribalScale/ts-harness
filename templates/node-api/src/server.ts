import { env } from './config/env.js';
import { prisma } from './db/prisma.js';
import { createApp } from './app.js';
import { logger } from './utils/logger.js';

const server = createApp().listen(env.PORT, () => {
  logger.info({ port: env.PORT }, 'API listening');
});

/** Finish in-flight requests, then close the database pool (ECS/Kubernetes send SIGTERM). */
function shutdown(signal: string) {
  logger.info({ signal }, 'shutting down');
  server.close(() => {
    prisma.$disconnect().finally(() => process.exit(0));
  });
  setTimeout(() => process.exit(1), 10_000).unref();
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
