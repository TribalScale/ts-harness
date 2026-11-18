import { PrismaClient } from '@prisma/client';

/** The one database client for the process. */
export const prisma = new PrismaClient();
