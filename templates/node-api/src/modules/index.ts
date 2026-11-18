import type { Module } from '../container.js';
import { healthModule } from './health/index.js';
import { notesModule } from './notes/index.js';

/** Every module the API serves. Add one line per new module. */
export const modules: Module[] = [healthModule, notesModule];
