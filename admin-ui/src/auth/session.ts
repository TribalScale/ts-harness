const LEGACY_TOKEN_KEY = "ec_admin_token";
const ROLES_KEY = "ec_admin_roles";
const PERMISSIONS_KEY = "ec_admin_permissions";
const PKCE_VERIFIER = "ec_pkce_verifier";

/** One-time read of a token left in localStorage by an older console. */
export function takeLegacyToken(): string {
  const token = localStorage.getItem(LEGACY_TOKEN_KEY) || "";
  if (token) localStorage.removeItem(LEGACY_TOKEN_KEY);
  return token;
}

/** Compact JWTs have three base64url segments. Opaque Auth0 / static tokens do not. */
export function isJwt(token: string): boolean {
  return token.split(".").filter(Boolean).length === 3;
}

export function pickJwt(...candidates: Array<string | null | undefined>): string {
  const tokens = candidates.filter((t): t is string => Boolean(t && t.trim()));
  return tokens.find(isJwt) || tokens[0] || "";
}

/** The token the gateway validates: the ID token when the provider is configured for it. */
export function sessionToken(
  tokenType: string | undefined,
  data: { access_token?: string | null; id_token?: string | null }
): string {
  if (tokenType === "id") return data.id_token || "";
  return pickJwt(data.access_token, data.id_token);
}

export function oidcTokenExchangeBody(opts: {
  code: string;
  redirectUri: string;
  clientId: string;
  codeVerifier: string;
  audience?: string;
}): URLSearchParams {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code: opts.code,
    redirect_uri: opts.redirectUri,
    client_id: opts.clientId,
    code_verifier: opts.codeVerifier,
  });
  if (opts.audience) body.set("audience", opts.audience);
  return body;
}

export async function clearSession(): Promise<void> {
  localStorage.removeItem(LEGACY_TOKEN_KEY);
  localStorage.removeItem(ROLES_KEY);
  localStorage.removeItem(PERMISSIONS_KEY);
  localStorage.removeItem(PKCE_VERIFIER);
  try {
    await fetch("/admin/auth/logout", { method: "POST", credentials: "include" });
  } catch {
    /* the cookie clear is best-effort if the gateway is down */
  }
}

export function setRoles(roles: string[]): void {
  localStorage.setItem(ROLES_KEY, JSON.stringify(roles));
}

export function getRoles(): string[] {
  try {
    return JSON.parse(localStorage.getItem(ROLES_KEY) || "[]");
  } catch {
    return [];
  }
}

export function setPermissions(permissions: string[]): void {
  localStorage.setItem(PERMISSIONS_KEY, JSON.stringify(permissions));
}

export function getPermissions(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(PERMISSIONS_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

export function hasPermission(permission: string): boolean {
  return getPermissions().includes(permission);
}

export function canManageSkills(): boolean {
  return hasPermission("edit_config");
}

function b64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let str = "";
  bytes.forEach((b) => {
    str += String.fromCharCode(b);
  });
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function createPkce(): Promise<{ verifier: string; challenge: string }> {
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  const verifier = b64url(array.buffer);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = b64url(digest);
  sessionStorage.setItem(PKCE_VERIFIER, verifier);
  return { verifier, challenge };
}

export function takePkceVerifier(): string {
  const v = sessionStorage.getItem(PKCE_VERIFIER) || "";
  sessionStorage.removeItem(PKCE_VERIFIER);
  return v;
}

/** Best-effort JWT role extraction for UI gating (server still enforces). */
export function rolesFromJwt(token: string): string[] {
  try {
    const payload = token.split(".")[1];
    if (!payload) return [];
    const json = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
    const namespaced = Object.entries(json).find(
      ([key]) => key === "roles" || key.endsWith("/roles")
    );
    const roles = json.roles || namespaced?.[1] || [];
    return Array.isArray(roles) ? roles.map(String) : [];
  } catch {
    return [];
  }
}
