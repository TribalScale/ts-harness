import type { Note, PrismaClient } from '@prisma/client';
import type { CreateNoteInput, ListNotesInput, UpdateNoteInput } from './notes.schemas.js';

/** Data access for notes. Services depend on this interface; tests can pass a fake. */
export interface NotesRepository {
  list(input: ListNotesInput): Promise<{ items: Note[]; total: number }>;
  findById(id: string): Promise<Note | null>;
  findByTitle(title: string): Promise<Note | null>;
  create(input: CreateNoteInput & { createdBy: string }): Promise<Note>;
  update(id: string, input: UpdateNoteInput): Promise<Note>;
  delete(id: string): Promise<void>;
}

export class PrismaNotesRepository implements NotesRepository {
  constructor(private readonly db: PrismaClient) {}

  async list({ page, limit, search }: ListNotesInput) {
    const where = search ? { title: { contains: search, mode: 'insensitive' as const } } : {};
    const [items, total] = await this.db.$transaction([
      this.db.note.findMany({
        where,
        orderBy: { createdAt: 'desc' },
        skip: (page - 1) * limit,
        take: limit,
      }),
      this.db.note.count({ where }),
    ]);
    return { items, total };
  }

  findById(id: string) {
    return this.db.note.findUnique({ where: { id } });
  }

  findByTitle(title: string) {
    return this.db.note.findFirst({ where: { title } });
  }

  create(input: CreateNoteInput & { createdBy: string }) {
    return this.db.note.create({ data: input });
  }

  update(id: string, input: UpdateNoteInput) {
    return this.db.note.update({ where: { id }, data: input });
  }

  async delete(id: string) {
    await this.db.note.delete({ where: { id } });
  }
}
