import { After, AfterAll, Before, setDefaultTimeout } from '@cucumber/cucumber';
import './env.js';

setDefaultTimeout(15_000);

const { prisma } = await import('../../src/db/prisma.js');

/** Each scenario starts from empty tables (migrations ran in `pretest:bdd`). */
Before(async function () {
  const tables = await prisma.$queryRaw<{ tablename: string }[]>`
    SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> '_prisma_migrations'`;
  if (tables.length) {
    const list = tables.map((t) => `"public"."${t.tablename}"`).join(', ');
    await prisma.$executeRawUnsafe(`TRUNCATE ${list} RESTART IDENTITY CASCADE`);
  }
});

After(function () {
  this.token = undefined;
});

AfterAll(async function () {
  await prisma.$disconnect();
});
