import type { PrismaClient } from '@prisma/client';

/**
 * Deterministic data for the nightly smoke run: the same rows, with the same ids, every time,
 * so a page that lists or opens a record has something real to render. Upserts only: running
 * it twice, or against a database that already has data, never deletes anything.
 *
 * Add a few rows per model as modules are added, enough for every page to show content. Dates
 * come from `src/lib/relativeDates.ts` (this week, next week), never a fixed calendar day, so the
 * data looks current on every run.
 */
export const SMOKE_NOTES = [
  {
    id: '00000000-0000-4000-8000-000000000001',
    title: 'Smoke: shift handover',
    body: 'Seeded by npm run start:smoke',
    createdBy: 'smoke-user',
  },
  {
    id: '00000000-0000-4000-8000-000000000002',
    title: 'Smoke: weekly plan',
    body: '',
    createdBy: 'smoke-admin',
  },
];

export async function seedSmoke(prisma: PrismaClient): Promise<void> {
  for (const note of SMOKE_NOTES) {
    await prisma.note.upsert({ where: { id: note.id }, create: note, update: note });
  }
}
