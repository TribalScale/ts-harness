import React, { useEffect, useState } from "react";
import { TextInput } from "@astryxdesign/core/TextInput";
import { apiFetch } from "../api/client";
import type { AuthConfig } from "../api/types";
import { Button } from "../components/Button";
import { ErrorBanner } from "../components/ErrorBanner";
import {
  createPkce,
  oidcTokenExchangeBody,
  pickJwt,
  setPermissions,
  setRoles,
  takePkceVerifier,
} from "./session";

type AuthConfigExt = AuthConfig & {
  allow_static_fallback?: boolean;
  error?: string;
};

export function LoginPage({ onAuthed }: { onAuthed: () => void }) {
  const [config, setConfig] = useState<AuthConfigExt | null>(null);
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function loadConfig() {
    setError(null);
    apiFetch<AuthConfigExt>("/admin/auth/config")
      .then(setConfig)
      .catch((e) => setError(String(e.message || e)));
  }

  useEffect(() => {
    loadConfig();
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (!code || !config || config.mode !== "oidc") return;
    const verifier = takePkceVerifier();
    if (!verifier || !config.token_endpoint || !config.client_id) return;
    setBusy(true);
    const body = oidcTokenExchangeBody({
      code,
      redirectUri: window.location.origin + window.location.pathname,
      clientId: config.client_id,
      codeVerifier: verifier,
      audience: config.audience ?? undefined,
    });
    fetch(config.token_endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`token exchange failed (${r.status})`);
        return r.json();
      })
      .then(async (data) => {
        const access = pickJwt(data.access_token, data.id_token);
        if (!access) throw new Error("no access_token in response");
        await establishSession(access);
        window.history.replaceState({}, "", window.location.pathname + "#/inbox");
        onAuthed();
      })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setBusy(false));
  }, [config, onAuthed]);

  async function startOidc() {
    if (!config?.authorization_endpoint || !config.client_id) {
      setError("OIDC is not fully configured (client_id / authorization_endpoint)");
      return;
    }
    const { challenge } = await createPkce();
    const url = new URL(config.authorization_endpoint);
    url.searchParams.set("response_type", "code");
    url.searchParams.set("client_id", config.client_id);
    url.searchParams.set("redirect_uri", window.location.origin + window.location.pathname);
    url.searchParams.set("scope", (config.scopes || ["openid"]).join(" "));
    url.searchParams.set("code_challenge", challenge);
    url.searchParams.set("code_challenge_method", "S256");
    if (config.audience) url.searchParams.set("audience", config.audience);
    window.location.href = url.toString();
  }

  async function establishSession(accessToken: string) {
    const session = await apiFetch<{ roles?: string[]; permissions?: string[] }>(
      "/admin/auth/session",
      { method: "POST", body: JSON.stringify({ access_token: accessToken }) }
    );
    localStorage.removeItem("ec_admin_token");
    setRoles(session.roles || []);
    setPermissions(session.permissions || []);
  }

  async function submitStatic(e: React.FormEvent) {
    e.preventDefault();
    if (!token.trim()) {
      setError("Token required");
      return;
    }
    setBusy(true);
    try {
      await establishSession(token.trim());
      window.history.replaceState({}, "", window.location.pathname + "#/inbox");
      onAuthed();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const showStatic =
    config?.mode === "static" ||
    (config?.mode === "oidc" && Boolean(config.allow_static_fallback));

  return (
    <div className="login">
      <h1>TS Harness</h1>
      <p className="muted">Sign in to the operator console</p>
      {error && <ErrorBanner message={error} onRetry={loadConfig} />}
      {config?.mode === "disabled" && (
        <ErrorBanner
          message={config.error || "Authentication is disabled. Configure OIDC."}
          onRetry={loadConfig}
        />
      )}
      {config?.mode === "oidc" && (
        <Button variant="primary" disabled={busy} onClick={startOidc}>
          {busy ? "Signing in…" : "Continue with SSO"}
        </Button>
      )}
      {showStatic && (
        <form className="login-form" onSubmit={submitStatic}>
          <TextInput
            label={
              config?.mode === "oidc"
                ? "Dev fallback: gateway bearer token"
                : "Gateway bearer token (local/dev)"
            }
            value={token}
            onChange={(value) => setToken(value)}
            placeholder="GATEWAY_TOKEN"
            autoComplete="off"
          />
          <Button variant="primary" type="submit">
            Enter console
          </Button>
        </form>
      )}
    </div>
  );
}
