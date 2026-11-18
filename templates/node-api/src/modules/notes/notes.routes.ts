import { Router } from 'express';
import { authenticate, requireRole } from '../../middlewares/auth.js';
import { validate } from '../../middlewares/validate.js';
import type { NotesController } from './notes.controller.js';
import { CreateNote, ListNotesQuery, NoteParams, UpdateNote } from './notes.schemas.js';

export function notesRouter(controller: NotesController): Router {
  const router = Router();
  router.use(authenticate);
  router.get('/', validate({ query: ListNotesQuery }), controller.list);
  router.post('/', validate({ body: CreateNote }), controller.create);
  router.get('/:id', validate({ params: NoteParams }), controller.get);
  router.patch('/:id', validate({ params: NoteParams, body: UpdateNote }), controller.update);
  router.delete('/:id', requireRole('Admin'), validate({ params: NoteParams }), controller.delete);
  return router;
}
