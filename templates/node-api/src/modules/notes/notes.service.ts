import type { Note } from '@prisma/client';
import { AppError } from '../../errors/appError.js';
import type { NotesRepository } from './notes.repository.js';
import type { CreateNoteInput, ListNotesInput, NoteDto, UpdateNoteInput } from './notes.schemas.js';

/** Business rules for notes. No HTTP or SQL here: that lives in the controller and repository. */
export class NotesService {
  constructor(private readonly repo: NotesRepository) {}

  async list(input: ListNotesInput) {
    const { items, total } = await this.repo.list(input);
    return { data: items.map(toDto), page: input.page, limit: input.limit, total };
  }

  async get(id: string): Promise<NoteDto> {
    const note = await this.repo.findById(id);
    if (!note) throw AppError.notFound('Note not found');
    return toDto(note);
  }

  async create(input: CreateNoteInput, createdBy: string): Promise<NoteDto> {
    if (await this.repo.findByTitle(input.title)) {
      throw AppError.conflict('A note with this title already exists');
    }
    return toDto(await this.repo.create({ ...input, createdBy }));
  }

  async update(id: string, input: UpdateNoteInput): Promise<NoteDto> {
    await this.get(id);
    return toDto(await this.repo.update(id, input));
  }

  async delete(id: string): Promise<void> {
    await this.get(id);
    await this.repo.delete(id);
  }
}

function toDto(note: Note): NoteDto {
  return {
    id: note.id,
    title: note.title,
    body: note.body,
    createdBy: note.createdBy,
    createdAt: note.createdAt.toISOString(),
    updatedAt: note.updatedAt.toISOString(),
  };
}
