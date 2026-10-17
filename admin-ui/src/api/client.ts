export class ApiError extends Error {
  status: number;
  code?: string;
  path?: string;
  constructor(status: number, message: string, extra: { code?: string; path?: string } = {}) {
    super(message);
    this.status = status;
    this.code = extra.code;
    this.path = extra.path;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  opts: { retries?: number } = {}
): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const retries = opts.retries ?? (method === "GET" ? 2 : 0);
  let lastError: unknown;

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const headers = new Headers(init.headers || {});
      if (!headers.has("Content-Type") && init.body) {
        headers.set("Content-Type", "application/json");
      }
      const res = await fetch(path, { ...init, headers, credentials: "include" });
      if (res.status === 401) onUnauthorized?.();
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        const extra: { code?: string; path?: string } = {};
        try {
          const body = await res.json();
          detail = body.error || detail;
          if (typeof body.error_code === "string") extra.code = body.error_code;
          if (typeof body.path === "string") extra.path = body.path;
        } catch {
          /* ignore */
        }
        // Retry transient failures on GET
        if (method === "GET" && res.status >= 500 && attempt < retries) {
          await sleep(300 * (attempt + 1));
          continue;
        }
        throw new ApiError(res.status, detail, extra);
      }
      if (res.status === 204) return {} as T;
      return (await res.json()) as T;
    } catch (err) {
      lastError = err;
      if (err instanceof ApiError) throw err;
      if (attempt < retries) {
        await sleep(300 * (attempt + 1));
        continue;
      }
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

/** Fetch a file download. Never retried: each successful call may be audited server-side. */
export async function apiDownload(path: string): Promise<{ blob: Blob; headers: Headers }> {
  const res = await fetch(path, { credentials: "include" });
  if (res.status === 401) onUnauthorized?.();
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.error || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return { blob: await res.blob(), headers: res.headers };
}
