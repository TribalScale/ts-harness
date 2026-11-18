import type { RequestHandler } from 'express';
import type { z } from 'zod';

type ZodTypeAny = z.ZodType;

interface Schemas {
  body?: ZodTypeAny;
  query?: ZodTypeAny;
  params?: ZodTypeAny;
}

export type Validated<S extends Schemas> = {
  body: S['body'] extends ZodTypeAny ? z.infer<S['body']> : undefined;
  query: S['query'] extends ZodTypeAny ? z.infer<S['query']> : undefined;
  params: S['params'] extends ZodTypeAny ? z.infer<S['params']> : undefined;
};

/**
 * Parse the request against Zod schemas and keep the typed result on `res.locals.valid`.
 * A failure throws a ZodError, which the error handler reports as 400 validation_failed.
 */
export function validate<S extends Schemas>(schemas: S): RequestHandler {
  return (req, res, next) => {
    res.locals.valid = {
      body: schemas.body?.parse(req.body),
      query: schemas.query?.parse(req.query),
      params: schemas.params?.parse(req.params),
    };
    next();
  };
}
