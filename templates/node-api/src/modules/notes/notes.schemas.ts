import { z } from 'zod';
import '../../config/openapi.js';

export const Note = z
  .object({
    id: z.uuid(),
    title: z.string(),
    body: z.string(),
    createdBy: z.string(),
    createdAt: z.iso.datetime(),
    updatedAt: z.iso.datetime(),
  })
  .openapi('Note');

export const CreateNote = z
  .object({
    title: z.string().trim().min(1, 'Title is required').max(200),
    body: z.string().max(10_000).default(''),
  })
  .openapi('CreateNote');

export const UpdateNote = CreateNote.partial().openapi('UpdateNote');

export const NoteParams = z.object({ id: z.uuid() });

export const ListNotesQuery = z.object({
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(100).default(20),
  search: z.string().trim().optional(),
});

export const NotePage = z
  .object({
    data: z.array(Note),
    page: z.number().int(),
    limit: z.number().int(),
    total: z.number().int(),
  })
  .openapi('NotePage');

export type NoteDto = z.infer<typeof Note>;
export type CreateNoteInput = z.infer<typeof CreateNote>;
export type UpdateNoteInput = z.infer<typeof UpdateNote>;
export type ListNotesInput = z.infer<typeof ListNotesQuery>;
