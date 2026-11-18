import { Router } from 'express';
import type { Module } from '../../container.js';

/** /health: the process is up. /ready: it can reach the database (for load balancers). */
export const healthModule: Module = (deps) => {
  const router = Router();
  router.get('/health', (_req, res) => {
    res.json({ status: 'ok' });
  });
  router.get('/ready', async (_req, res) => {
    try {
      await deps.prisma.$queryRaw`SELECT 1`;
      res.json({ status: 'ready' });
    } catch {
      res.status(503).json({ status: 'unavailable' });
    }
  });
  return { path: '/', router };
};
