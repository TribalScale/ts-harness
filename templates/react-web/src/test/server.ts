import { setupServer } from 'msw/node';

/** Mock API for unit tests. Each test adds the handlers it needs with server.use(...). */
export const server = setupServer();
export const API = 'http://api.test';
