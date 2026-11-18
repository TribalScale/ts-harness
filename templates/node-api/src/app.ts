import cors from 'cors';
import express from 'express';
import helmet from 'helmet';
import { pinoHttp } from 'pino-http';
import { randomUUID } from 'node:crypto';
import swaggerUi from 'swagger-ui-express';
import { env } from './config/env.js';
import { openApiDocument } from './config/openapi.js';
import { createDeps, type Deps } from './container.js';
import { errorHandler, notFound } from './middlewares/errorHandler.js';
import { modules } from './modules/index.js';
import { logger } from './utils/logger.js';

export function createApp(deps: Deps = createDeps()) {
  const app = express();
  app.disable('x-powered-by');
  app.use(
    pinoHttp({
      logger,
      genReqId: (req, res) => {
        const id = (req.headers['x-request-id'] as string) || randomUUID();
        res.setHeader('x-request-id', id);
        return id;
      },
    }),
  );
  app.use(helmet());
  app.use(cors({ origin: env.CORS_ORIGINS, credentials: true }));
  app.use(express.json({ limit: '1mb' }));

  for (const mod of modules) {
    const { path, router } = mod(deps);
    app.use(path, router);
  }

  const document = openApiDocument();
  app.get('/openapi.json', (_req, res) => {
    res.json(document);
  });
  app.use('/docs', swaggerUi.serve, swaggerUi.setup(document));

  app.use(notFound);
  app.use(errorHandler);
  return app;
}
