import type { Module } from '../../container.js';
import { NotesController } from './notes.controller.js';
import { PrismaNotesRepository } from './notes.repository.js';
import { notesRouter } from './notes.routes.js';
import { NotesService } from './notes.service.js';
import './notes.openapi.js';

/** Reference module: copy its shape for new resources. */
export const notesModule: Module = (deps) => {
  const service = new NotesService(new PrismaNotesRepository(deps.prisma));
  return { path: '/notes', router: notesRouter(new NotesController(service)) };
};
