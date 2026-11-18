/**
 * An error the API reports to the caller. Throw it from services and controllers; the error
 * handler turns it into `{ error: { code, message, details } }` with `status`.
 */
export class AppError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = 'AppError';
  }

  static badRequest(message: string, details?: unknown) {
    return new AppError(400, 'bad_request', message, details);
  }

  static unauthorized(message = 'Authentication required') {
    return new AppError(401, 'unauthorized', message);
  }

  static forbidden(message = 'Permission denied') {
    return new AppError(403, 'forbidden', message);
  }

  static notFound(message = 'Not found') {
    return new AppError(404, 'not_found', message);
  }

  static conflict(message: string) {
    return new AppError(409, 'conflict', message);
  }
}
