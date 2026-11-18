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

  it('lists a page of notes with the total', async () => {
    const service = new NotesService(fakeRepo());
    await service.create({ title: 'One', body: '' }, 'user-1');
    await service.create({ title: 'Two', body: '' }, 'user-1');
    const page = await service.list({ page: 1, limit: 20 });
    expect(page).toMatchObject({ page: 1, limit: 20, total: 2 });
    expect(page.data.map((n) => n.title).sort()).toEqual(['One', 'Two']);
  });

  it('updates a note', async () => {
    const service = new NotesService(fakeRepo());
    const note = await service.create({ title: 'Draft', body: '' }, 'user-1');
    const updated = await service.update(note.id, { body: 'Final' });
    expect(updated).toMatchObject({ id: note.id, title: 'Draft', body: 'Final' });
  });

  it('refuses to update or delete a note that does not exist', async () => {
    const service = new NotesService(fakeRepo());
    const missing = crypto.randomUUID();
    await expect(service.update(missing, { body: 'x' })).rejects.toMatchObject({ status: 404 });
    await expect(service.delete(missing)).rejects.toMatchObject({ status: 404 });
  });

  it('deletes a note', async () => {
    const service = new NotesService(fakeRepo());
    const note = await service.create({ title: 'Old', body: '' }, 'user-1');
    await service.delete(note.id);
    await expect(service.get(note.id)).rejects.toMatchObject({ status: 404 });
  });

  it('reports a missing note as not found', async () => {
    const service = new NotesService(fakeRepo());
    const error = await service.get(crypto.randomUUID()).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AppError);
    expect((error as AppError).status).toBe(404);
  });
});
