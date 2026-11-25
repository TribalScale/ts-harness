import { existsSync, readFileSync } from 'node:fs';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

/**
 * The backend's contract (its openapi.json), checked on every request the app makes to the
 * fake API in an acceptance test. A path or method the backend does not have, a request body
 * it would reject, or a fake response it would never send fails the scenario, so the fake
 * cannot drift from the real API unnoticed.
 *
 * The spec is `OPENAPI_CONTRACT` when set (TS Harness points it at the backend's current
 * openapi.json), else the copy in src/api/openapi.json (`npm run api:pull`).
 */
type Operation = {
  requestBody?: { content?: Record<string, { schema?: unknown }> };
  responses?: Record<string, { content?: Record<string, { schema?: unknown }> }>;
};
type Spec = { paths?: Record<string, Record<string, Operation>>; components?: unknown };

const SPEC_PATH = process.env.OPENAPI_CONTRACT || 'src/api/openapi.json';

let loaded:
  | { spec: Spec; ajv: Ajv2020; routes: { method: string; path: string; regex: RegExp }[] }
  | null
  | undefined;

function load() {
  if (loaded !== undefined) return loaded;
  if (!existsSync(SPEC_PATH)) {
    console.warn(
      `contract: ${SPEC_PATH} not found; the fake API is not checked against the backend`,
    );
    loaded = null;
    return loaded;
  }
  const spec = JSON.parse(readFileSync(SPEC_PATH, 'utf8')) as Spec;
  const ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  ajv.addSchema({ components: spec.components ?? {} }, 'spec');
  const routes = Object.entries(spec.paths ?? {}).flatMap(([path, ops]) =>
    Object.keys(ops).map((method) => ({
      method: method.toUpperCase(),
      path,
      regex: new RegExp(
        '^' + path.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{[^/}]+\}/g, '[^/]+') + '$',
      ),
    })),
  );
  loaded = { spec, ajv, routes };
  return loaded;
}

/** `#/components/...` refs point into the schema registered as `spec`. */
function rebase(schema: unknown): unknown {
  if (Array.isArray(schema)) return schema.map(rebase);
  if (schema && typeof schema === 'object') {
    return Object.fromEntries(
      Object.entries(schema).map(([key, value]) => [
        key,
        key === '$ref' && typeof value === 'string' && value.startsWith('#/')
          ? `spec${value}`
          : rebase(value),
      ]),
    );
  }
  return schema;
}

function invalid(ajv: Ajv2020, schema: unknown, data: unknown): string | null {
  const validate = ajv.compile(rebase(schema) as object);
  if (validate(data)) return null;
  return (validate.errors ?? [])
    .map((e) => `${e.instancePath || '(body)'} ${e.message}`)
    .join('; ');
}

/** Problems with one request/response pair, empty when the backend's contract allows it. */
export function contractProblems(
  method: string,
  pathname: string,
  requestBody: unknown,
  status: number,
  responseBody: unknown,
): string[] {
  const contract = load();
  if (!contract) return [];
  const route = contract.routes.find((r) => r.method === method && r.regex.test(pathname));
  if (!route) return [`${method} ${pathname}: the backend's openapi.json has no such operation`];
  const operation = contract.spec.paths![route.path][route.method.toLowerCase()];
  const problems: string[] = [];
  const requestSchema = operation.requestBody?.content?.['application/json']?.schema;
  if (requestSchema && requestBody !== undefined) {
    const why = invalid(contract.ajv, requestSchema, requestBody);
    if (why) problems.push(`${method} ${pathname} request: ${why}`);
  }
  const response = operation.responses?.[String(status)] ?? operation.responses?.default;
  if (!response) {
    problems.push(`${method} ${pathname}: status ${status} is not documented`);
  } else {
    const responseSchema = response.content?.['application/json']?.schema;
    if (responseSchema && responseBody !== undefined) {
      const why = invalid(contract.ajv, responseSchema, responseBody);
      if (why) problems.push(`${method} ${pathname} ${status} response: ${why}`);
    }
  }
  return problems;
}
