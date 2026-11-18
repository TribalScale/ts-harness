import type { Request, Response } from 'express';
import type { NotesService } from './notes.service.js';
import type { CreateNoteInput, ListNotesInput, UpdateNoteInput } from './notes.schemas.js';

/** HTTP in, HTTP out. Inputs are already validated (res.locals.valid); errors are thrown. */
export class NotesController {
  constructor(private readonly service: NotesService) {}

  list = async (_req: Request, res: Response) => {
    const { query } = res.locals.valid as { query: ListNotesInput };
    res.json(await this.service.list(query));
  };

  get = async (_req: Request, res: Response) => {
    const { params } = res.locals.valid as { params: { id: string } };
    res.json(await this.service.get(params.id));
  };

  create = async (req: Request, res: Response) => {
    const { body } = res.locals.valid as { body: CreateNoteInput };
    res.status(201).json(await this.service.create(body, req.principal!.id));
  };

  update = async (_req: Request, res: Response) => {
    const { params, body } = res.locals.valid as { params: { id: string }; body: UpdateNoteInput };
    res.json(await this.service.update(params.id, body));
  };

  delete = async (_req: Request, res: Response) => {
    const { params } = res.locals.valid as { params: { id: string } };
    await this.service.delete(params.id);
    res.status(204).end();
  };
}
