import type { PrismaClient } from '@prisma/client';

/**
 * Reference data a fresh environment needs before its features can work: lookup tables, rules,
 * calendars, standards, sample configuration. Without it a new dev environment (or the nightly
 * smoke run) starts empty and the screens that depend on it show nothing, so nobody, people or
 * automated checks, sees what the app really does.
 *
 * `npm run seed:dev` runs it; `npm run start:smoke` runs it before the smoke data. Upserts with
 * fixed ids only: running it twice, or against a database that already has data, changes
 * nothing. It refuses NODE_ENV=production. Dates come from `src/lib/relativeDates.ts`, never a
 * fixed calendar day.
 *
 * Add a sample here whenever a feature needs configuration data to do anything.
 */
export async function seedDev(prisma: PrismaClient): Promise<void> {
  // The template has no reference data yet. A module that needs some upserts it here, e.g.
  // await prisma.calendarRule.upsert({ where: { id: 1 }, create: SAMPLE_RULE, update: SAMPLE_RULE });
  void prisma;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  if (process.env.NODE_ENV === 'production') {
    console.error('seed:dev refuses NODE_ENV=production: it writes sample reference data.');
    process.exit(1);
  }
  const { prisma } = await import('../src/db/prisma.js');
  await seedDev(prisma);
  await prisma.$disconnect();
  console.log('dev reference data seeded');
}
