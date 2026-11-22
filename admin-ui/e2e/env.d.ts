// The Playwright config reads a few environment variables; this avoids pulling in @types/node.
declare const process: { env: Record<string, string | undefined> };
