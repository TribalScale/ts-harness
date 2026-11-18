import assert from 'node:assert/strict';
import { Given, Then, When } from '@cucumber/cucumber';
import request from 'supertest';
import '../support/env.js';
import { PERSONAS, testToken } from '../support/auth.js';
import type { ApiWorld } from '../support/world.js';

/**
 * Generic HTTP steps every feature can use. Add resource-specific steps in their own file
 * (features/step_definitions/<resource>.steps.ts) only when these do not read well.
 */
const { createApp } = await import('../../src/app.js');
const app = createApp();

Given('I am signed in as an/a {word}', async function (this: ApiWorld, persona: string) {
  const roles = PERSONAS[persona];
  assert.ok(roles, `Unknown persona "${persona}". Add it to features/support/auth.ts`);
  this.token = await testToken({ id: `${persona}-1`, name: persona, roles });
});

Given('I am not signed in', function (this: ApiWorld) {
  this.token = undefined;
});

When(
  'I send a {word} request to {string}',
  async function (this: ApiWorld, method: string, path: string) {
    this.response = await send(this, method, path);
  },
);

When(
  'I send a {word} request to {string} with:',
  async function (this: ApiWorld, method: string, path: string, body: string) {
    this.response = await send(this, method, path, JSON.parse(this.fill(body)));
  },
);

Then('the response status should be {int}', function (this: ApiWorld, status: number) {
  assert.equal(this.response?.status, status, `body: ${JSON.stringify(this.response?.body)}`);
});

Then(
  'the response field {string} should be {string}',
  function (this: ApiWorld, path: string, expected: string) {
    assert.equal(String(read(this.response?.body, path)), this.fill(expected));
  },
);

Then(
  'the response field {string} should be {int}',
  function (this: ApiWorld, path: string, expected: number) {
    assert.equal(read(this.response?.body, path), expected);
  },
);

Then('the error code should be {string}', function (this: ApiWorld, code: string) {
  assert.equal(this.response?.body?.error?.code, code);
});

Then(
  'I remember the response field {string} as {word}',
  function (this: ApiWorld, path: string, name: string) {
    this.saved[name] = read(this.response?.body, path);
  },
);

async function send(world: ApiWorld, method: string, path: string, body?: unknown) {
  const verb = method.toLowerCase() as 'get' | 'post' | 'put' | 'patch' | 'delete';
  let req = request(app)[verb](world.fill(path));
  if (world.token) req = req.set('Authorization', `Bearer ${world.token}`);
  return body === undefined ? req : req.send(body as object);
}

/** Read "data.0.title" style paths. */
function read(body: unknown, path: string): unknown {
  return path
    .split('.')
    .reduce<unknown>((value, key) => (value as Record<string, unknown>)?.[key], body);
}
