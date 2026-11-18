import { z } from 'zod';
import { bearerAuth, ErrorResponse, registry } from '../../config/openapi.js';
import {
  CreateNote,
  ListNotesQuery,
  Note,
  NotePage,
  NoteParams,
  UpdateNote,
} from './notes.schemas.js';

const security = [{ [bearerAuth.name]: [] }];
const json = <T extends z.ZodType>(schema: T, description: string) => ({
  description,
  content: { 'application/json': { schema } },
});
const errors = {
  400: json(ErrorResponse, 'Validation failed'),
  401: json(ErrorResponse, 'Not signed in'),
  404: json(ErrorResponse, 'Note not found'),
};

registry.registerPath({
  method: 'get',
  path: '/notes',
  tags: ['Notes'],
  security,
  operationId: 'listNotes',
  request: { query: ListNotesQuery },
  responses: { 200: json(NotePage, 'A page of notes'), 401: errors[401] },
});
registry.registerPath({
  method: 'post',
  path: '/notes',
  tags: ['Notes'],
  security,
  operationId: 'createNote',
  request: { body: { content: { 'application/json': { schema: CreateNote } } } },
  responses: {
    201: json(Note, 'Created'),
    400: errors[400],
    401: errors[401],
    409: json(ErrorResponse, 'Title taken'),
  },
});
registry.registerPath({
  method: 'get',
  path: '/notes/{id}',
  tags: ['Notes'],
  security,
  operationId: 'getNote',
  request: { params: NoteParams },
  responses: { 200: json(Note, 'The note'), ...errors },
});
registry.registerPath({
  method: 'patch',
  path: '/notes/{id}',
  tags: ['Notes'],
  security,
  operationId: 'updateNote',
  request: {
    params: NoteParams,
    body: { content: { 'application/json': { schema: UpdateNote } } },
  },
  responses: { 200: json(Note, 'Updated'), ...errors },
});
registry.registerPath({
  method: 'delete',
  path: '/notes/{id}',
  tags: ['Notes'],
  security,
  operationId: 'deleteNote',
  request: { params: NoteParams },
  responses: {
    204: { description: 'Deleted' },
    403: json(ErrorResponse, 'Admin role required'),
    ...errors,
  },
});
