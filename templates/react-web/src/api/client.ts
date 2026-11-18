import createClient, { type Middleware } from 'openapi-fetch';
import { env } from '../config/env';
import type { paths } from './schema';

/** An error response from the API: `{ error: { code, message, details } }`. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

let tokenProvider: () => Promise<string | null> = async () => null;

/** Called once by the app shell so every request carries the signed-in user's token. */
export function setTokenProvider(provider: () => Promise<string | null>) {
  tokenProvider = provider;
}

const auth: Middleware = {
  async onRequest({ request }) {
    const token = await tokenProvider();
    if (token) request.headers.set('Authorization', `Bearer ${token}`);
    return request;
  },
};

/**
 * Typed client generated from the backend's openapi.json (`npm run api:generate`).
 * Paths, parameters and response bodies are all checked by TypeScript.
 */
export const api = createClient<paths>({
  baseUrl: env.VITE_API_URL,
  // Look fetch up on each call, so test mocks installed after this module loads still apply.
  fetch: (request) => globalThis.fetch(request),
});
api.use(auth);

/** Unwrap an openapi-fetch result: the data, or a thrown ApiError. */
export async function call<T>(
  promise: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await promise;
  if (error !== undefined || !response.ok) {
    const body = (error ?? {}) as {
      error?: { code?: string; message?: string; details?: unknown };
    };
    throw new ApiError(
      response.status,
      body.error?.code ?? 'http_error',
      body.error?.message ?? `Request failed (${response.status})`,
      body.error?.details,
    );
  }
  return data as T;
}
