import { setWorldConstructor, World, type IWorldOptions } from '@cucumber/cucumber';
import type { Response } from 'supertest';

/** Per-scenario state. Steps share data through `this`, never through module variables. */
export class ApiWorld extends World {
  token?: string;
  response?: Response;
  /** Values a scenario saved by name ("And I remember the response id as note"). */
  saved: Record<string, unknown> = {};

  constructor(options: IWorldOptions) {
    super(options);
  }

  /** Replace {name} in a path or body with values saved earlier in the scenario. */
  fill(text: string): string {
    return text.replace(/\{(\w+)\}/g, (match, key: string) =>
      key in this.saved ? String(this.saved[key]) : match,
    );
  }
}

setWorldConstructor(ApiWorld);
