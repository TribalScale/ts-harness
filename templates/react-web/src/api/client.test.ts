import { api } from './client';

// The client is typed by the paths generated from openapi.json, so `npm run typecheck` fails
// on a call to an endpoint the backend does not have. If the ts-expect-error lines below stop
// being errors, the client has lost its types.
describe('api client types', () => {
  it('accepts paths in the spec and rejects the rest', () => {
    const calls = [
      () => api.GET('/notes', { params: { query: { page: 1, limit: 10 } } }),
      () => api.DELETE('/notes/{id}', { params: { path: { id: 'n1' } } }),
      // @ts-expect-error '/not-in-the-spec' is not a path in openapi.json
      () => api.GET('/not-in-the-spec'),
      // @ts-expect-error '/notes' has no PUT in openapi.json
      () => api.PUT('/notes', { body: {} }),
    ];
    expect(calls).toHaveLength(4);
  });
});
