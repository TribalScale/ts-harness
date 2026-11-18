import { pino } from 'pino';
import { env } from '../config/env.js';

/** Structured JSON logs. Pass context as an object: logger.info({ noteId }, 'note created'). */
export const logger = pino({
  level: env.NODE_ENV === 'test' ? 'silent' : env.LOG_LEVEL,
  redact: ['req.headers.authorization', 'req.headers.cookie'],
});
