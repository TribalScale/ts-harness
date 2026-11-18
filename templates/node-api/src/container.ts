import type { PrismaClient } from '@prisma/client';
import type { Router } from 'express';
import { prisma } from './db/prisma.js';

/** Shared infrastructure every module may use. Add clients (S3, SQS, SAP...) here. */
export interface Deps {
  prisma: PrismaClient;
}

/** A feature module: builds its own service graph from `Deps` and returns its routes. */
export type Module = (deps: Deps) => { path: string; router: Router };

export function createDeps(overrides: Partial<Deps> = {}): Deps {
  return { prisma, ...overrides };
}
