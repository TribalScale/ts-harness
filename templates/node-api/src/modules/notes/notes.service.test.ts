import type { Note } from '@prisma/client';
import { describe, expect, it } from 'vitest';
import { AppError } from '../../errors/appError.js';
import type { NotesRepository } from './notes.repository.js';
import { NotesService } from './notes.service.js';

/** In-memory repository: unit tests cover business rules without a database. */
function fakeRepo(seed: Note[] = []): NotesRepository {
  const notes = new Map(seed.map((n) => [n.id, n]));
  return {
    async list() {
      return { items: [...notes.values()], total: notes.size };
    },
    async findById(id) {
      return notes.get(id) ?? null;
    },
    async findByTitle(title) {
      return [...notes.values()].find((n) => n.title === title) ?? null;
    },
    async create(input) {
      const now = new Date();
      const note: Note = { id: crypto.randomUUID(), ...input, createdAt: now, updatedAt: now };
      notes.set(note.id, note);
      return note;
    },
    async update(id, input) {
      const note = { ...notes.get(id)!, ...input, updatedAt: new Date() } as Note;
      notes.set(id, note);
      return note;
    },
    async delete(id) {
      notes.delete(id);
    },
  };
}

describe('NotesService', () => {
  it('creates a note owned by the caller', async () => {
    const service = new NotesService(fakeRepo());
    const note = await service.create({ title: 'First', body: 'Hello' }, 'user-1');
    expect(note).toMatchObject({ title: 'First', body: 'Hello', createdBy: 'user-1' });
  });

  it('refuses a duplicate title', async () => {
    const service = new NotesService(fakeRepo());
    await service.create({ title: 'Same', body: '' }, 'user-1');
    await expect(service.create({ title: 'Same', body: '' }, 'user-2')).rejects.toMatchObject({
      status: 409,
    });
  });

  it('reports a missing note as not found', async () => {
    const service = new NotesService(fakeRepo());
    const error = await service.get(crypto.randomUUID()).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AppError);
    expect((error as AppError).status).toBe(404);
  });
});
