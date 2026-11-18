import type { ErrorRequestHandler, RequestHandler } from 'express';
import { ZodError } from 'zod';
import { AppError } from '../errors/appError.js';
import { logger } from '../utils/logger.js';

/** 404 for routes nobody handles. */
export const notFound: RequestHandler = (req, _res, next) => {
  next(AppError.notFound(`No route for ${req.method} ${req.path}`));
};

/** The single place errors become responses. Unknown errors never leak their message. */
export const errorHandler: ErrorRequestHandler = (err, req, res, _next) => {
  if (err instanceof ZodError) {
    res.status(400).json({
      error: {
        code: 'validation_failed',
        message: 'Validation failed',
        details: err.issues.map((i) => ({ path: i.path.join('.'), message: i.message })),
      },
    });
    return;
  }
  if (err instanceof AppError) {
    if (err.status >= 500) logger.error({ err, reqId: req.id }, err.message);
    res
      .status(err.status)
      .json({ error: { code: err.code, message: err.message, details: err.details } });
    return;
  }
  logger.error({ err, reqId: req.id }, 'unhandled error');
  res.status(500).json({ error: { code: 'internal', message: 'An unexpected error occurred' } });
};
