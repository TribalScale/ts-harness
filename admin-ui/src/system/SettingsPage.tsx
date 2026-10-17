import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { Button } from "../components/Button";
import { ErrorBanner } from "../components/ErrorBanner";
import { PageState } from "../components/PageState";
import { Tabs, tabButtonId, tabPanelId } from "../components/Tabs";
import { AdvancedYaml } from "./settings/AdvancedYaml";
import {
  BindingsForm,
  ConnectorForm,
  DeliveryExtras,
  IdentityExtras,
  PolicyForm,
  SandboxForm,
  ToolsetsForm,
} from "./settings/ControlsForms";
import { IdentityForm } from "./settings/IdentityForm";
import { JiraDefaultsForm } from "./settings/JiraDefaultsForm";
import { RoutingForm } from "./settings/RoutingForm";
import type {
  RoutingDraft,
  RoutingGet,
  RuntimeSettingsDefinition,
  RuntimeSettingsDraft,
  RuntimeSettingsGet,
  SettingsSection,
} from "./settings/types";

const TABS_ID = "settings-sections";

function emptyDefinition(): RuntimeSettingsDefinition {
  return { definition_type: "runtime_settings", definition_version: "1.0.0" };
}

export function SettingsPage({
  section = "defaults",
  onDirtyChange,
}: {
  section?: SettingsSection;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [payload, setPayload] = useState<RuntimeSettingsGet | null>(null);
  const [draft, setDraft] = useState<RuntimeSettingsDraft | null>(null);
  const [definition, setDefinition] = useState<RuntimeSettingsDefinition>(emptyDefinition());
  const [routing, setRouting] = useState<RoutingGet | null>(null);
  const [routingDraft, setRoutingDraft] = useState<RoutingDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [routingDirty, setRoutingDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validated, setValidated] = useState(false);
  const [issues, setIssues] = useState<Array<{ severity?: string; message?: string; code?: string }>>(
    []
  );
  const [busy, setBusy] = useState(false);
  const routingLoadedRef = useRef(false);
  const settingsTouchedRef = useRef(false);
  const routingTouchedRef = useRef(false);

  const markDirty = useCallback(
    (next: boolean) => {
      setDirty(next);
      if (next) {
        settingsTouchedRef.current = true;
        setValidated(false);
      }
      onDirtyChange?.(next || routingDirty);
    },
    [onDirtyChange, routingDirty]
  );

  const markRoutingDirty = useCallback(
    (next: boolean) => {
      setRoutingDirty(next);
      if (next) {
        routingTouchedRef.current = true;
        setValidated(false);
      }
      onDirtyChange?.(dirty || next);
    },
    [onDirtyChange, dirty]
  );

  const load = useCallback(async () => {
    setError(null);
    const data = await apiFetch<RuntimeSettingsGet>("/admin/runtime-settings");
    setPayload(data);
    let nextDraft = data.draft;
    if (!nextDraft) {
      const created = await apiFetch<{ draft: RuntimeSettingsDraft }>(
        "/admin/runtime-settings/drafts",
        {
          method: "POST",
          body: JSON.stringify({ definition: data.definition || emptyDefinition() }),
        }
      );
      nextDraft = created.draft;
    }
    setDraft(nextDraft);
    setDefinition(nextDraft.definition || emptyDefinition());
    setDirty(false);
    settingsTouchedRef.current = false;
    onDirtyChange?.(false);
  }, [onDirtyChange]);

  const loadRouting = useCallback(async (force = false) => {
    if (routingLoadedRef.current && !force) return;
    const data = await apiFetch<RoutingGet>("/admin/routing-definitions");
    setRouting(data);
    if (data.draft) {
      setRoutingDraft(data.draft);
    } else {
      const created = await apiFetch<{ draft: RoutingDraft }>("/admin/routing-definitions/drafts", {
        method: "POST",
        body: JSON.stringify({
          definition: data.definition || {
            definition_type: "channel_routing",
            definition_version: "1.0.0",
            intents: [],
          },
        }),
      });
      setRoutingDraft(created.draft);
    }
    routingLoadedRef.current = true;
    setRoutingDirty(false);
  }, []);

  useEffect(() => {
    void load().catch((e: Error) => setError(e.message));
  }, [load]);

  useEffect(() => {
    if (section === "routing" || section === "advanced") {
      void loadRouting().catch((e: Error) => setError(e.message));
    }
  }, [section, loadRouting]);

  async function refreshPayload() {
    const refreshed = await apiFetch<RuntimeSettingsGet>("/admin/runtime-settings");
    setPayload(refreshed);
  }

  async function persistSettings() {
    if (!draft) return draft;
    const saved = await apiFetch<{ draft: RuntimeSettingsDraft }>(
      `/admin/runtime-settings/drafts/${encodeURIComponent(draft.id)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          definition,
          expected_revision: draft.revision,
        }),
      }
    );
    setDraft(saved.draft);
    setDefinition(saved.draft.definition);
    markDirty(false);
    try {
      await refreshPayload();
    } catch {
      /* keep the local payload if refresh fails */
    }
    return saved.draft;
  }

  async function persistRouting() {
    if (!routingDraft) return routingDraft;
    const saved = await apiFetch<{ draft: RoutingDraft }>(
      `/admin/routing-definitions/drafts/${encodeURIComponent(routingDraft.id)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          definition: routingDraft.definition,
          expected_revision: routingDraft.revision,
        }),
      }
    );
    setRoutingDraft(saved.draft);
    markRoutingDirty(false);
    try {
      await refreshPayload();
      const data = await apiFetch<RoutingGet>("/admin/routing-definitions");
      setRouting({ ...data, draft: saved.draft });
    } catch {
      /* keep the local routing payload if refresh fails */
    }
    return saved.draft;
  }

  async function onValidate() {
    setBusy(true);
    setError(null);
    setValidated(false);
    try {
      const saved = await persistSettings();
      const result = await apiFetch<{
        valid: boolean;
        issues: Array<{ severity?: string; message?: string; code?: string }>;
      }>(`/admin/runtime-settings/drafts/${encodeURIComponent(saved!.id)}/validate`, {
        method: "POST",
      });
      let nextIssues = result.issues || [];
      if (routingDraft && (section === "routing" || routingDirty || routingTouchedRef.current)) {
        const routingSaved = await persistRouting();
        const routingResult = await apiFetch<{
          issues: Array<{ severity?: string; message?: string; code?: string }>;
        }>(`/admin/routing-definitions/drafts/${encodeURIComponent(routingSaved!.id)}/validate`, {
          method: "POST",
        });
        nextIssues = [...nextIssues, ...(routingResult.issues || [])];
      }
      setIssues(nextIssues);
      if (!nextIssues.some((issue) => issue.severity === "error")) {
        setValidated(true);
      }
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function onActivate() {
    setBusy(true);
    setError(null);
    try {
      const includeSettings = dirty || settingsTouchedRef.current;
      const includeRouting = routingDirty || routingTouchedRef.current;
      let saved = draft;
      let routingSaved = routingDraft;
      if (includeSettings) {
        saved = await persistSettings();
      }
      if (includeRouting) {
        routingSaved = await persistRouting();
      }
      const body: Record<string, string> = {};
      if (includeSettings && saved) {
        body.runtime_settings_draft_id = saved.id;
        body.runtime_settings_expected_revision = saved.revision;
      }
      if (includeRouting && routingSaved) {
        body.routing_draft_id = routingSaved.id;
        body.routing_expected_revision = routingSaved.revision;
      }
      const result = await apiFetch<{
        reload_ok: boolean;
        issues?: Array<{ severity?: string; message?: string; code?: string }>;
      }>("/admin/runtime-settings/activate", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setIssues(result.issues || []);
      if (!result.reload_ok) {
        setError("Activate applied last-known-good; reload_ok is false.");
      }
      settingsTouchedRef.current = false;
      routingTouchedRef.current = false;
      routingLoadedRef.current = false;
      await load();
      if (includeRouting || routing) {
        await loadRouting(true);
      }
      setRoutingDirty(false);
      onDirtyChange?.(false);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  if (error && !payload) {
    return (
      <PageState kind="error" title="Unable to load settings" detail={error} onRetry={() => void load()} />
    );
  }
  if (!payload || !draft) {
    return <PageState kind="loading" title="Loading settings" />;
  }

  const warnings = issues.filter((issue) => issue.severity === "warning");
  const errors = issues.filter((issue) => issue.severity === "error");
  const settingsActive =
    payload.active_revision?.slice(0, 8) || payload.definition?.definition_version || "";
  const routingActive =
    routing?.active_revision?.slice(0, 8) || routing?.definition?.definition_version || "";

  return (
    <section className="settings-page" aria-label="Tenant defaults">
      <div className="system-header">
        <div>
          <h3>Tenant defaults</h3>
          <p className="muted">
            Draft {dirty || routingDirty ? "dirty" : "saved"} · runtime-settings draft{" "}
            {draft.revision.slice(0, 8)}
            {settingsActive ? ` · active ${settingsActive}` : ""}
            {routingDraft ? ` · routing draft ${routingDraft.revision.slice(0, 8)}` : ""}
            {routingActive ? ` · routing active ${routingActive}` : ""}
          </p>
        </div>
        <div className="settings-actions">
          <Button variant="secondary" busy={busy} onClick={() => void onValidate()}>
            Validate
          </Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={errors.length > 0}
            onClick={() => void onActivate()}
          >
            Activate
          </Button>
        </div>
      </div>
      {error && <ErrorBanner message={error} />}
      {validated && errors.length === 0 && (
        <p className="success" role="status">
          Validated
        </p>
      )}
      {warnings.map((issue) => (
        <p key={issue.message} className="muted" role="status">
          {issue.message}
        </p>
      ))}
      {errors.map((issue) => (
        <ErrorBanner key={issue.message} message={issue.message || "Validation error"} />
      ))}
      <Tabs
        idBase={TABS_ID}
        ariaLabel="Settings sections"
        active={section}
        tabs={[
          { id: "defaults", label: "Jira defaults" },
          { id: "identity", label: "Identity" },
          { id: "routing", label: "Routing" },
          { id: "policy", label: "Policy" },
          { id: "sandbox", label: "Sandbox" },
          { id: "channels", label: "Channels" },
          { id: "toolsets", label: "Toolsets" },
          { id: "connector", label: "Connector" },
          { id: "advanced", label: "Advanced" },
        ]}
        onChange={(id) => {
          window.location.hash =
            id === "defaults" ? "#/system/settings" : `#/system/settings/${id}`;
        }}
      />
      <div
        role="tabpanel"
        id={tabPanelId(TABS_ID, section)}
        aria-labelledby={tabButtonId(TABS_ID, section)}
        tabIndex={0}
      >
        {section === "defaults" && (
          <>
            <JiraDefaultsForm
              merged={payload.merged}
              sources={payload.sources}
              definition={definition}
              onChange={(next) => {
                setDefinition(next);
                markDirty(true);
              }}
            />
            <DeliveryExtras
              merged={payload.merged}
              definition={definition}
              onChange={(next) => {
                setDefinition(next);
                markDirty(true);
              }}
            />
          </>
        )}
        {section === "identity" && (
          <>
            <IdentityForm
              merged={payload.merged}
              sources={payload.sources}
              definition={definition}
              onChange={(next) => {
                setDefinition(next);
                markDirty(true);
              }}
            />
            <IdentityExtras
              merged={payload.merged}
              definition={definition}
              onChange={(next) => {
                setDefinition(next);
                markDirty(true);
              }}
            />
          </>
        )}
        {section === "policy" && (
          <PolicyForm
            merged={payload.merged}
            definition={definition}
            onChange={(next) => {
              setDefinition(next);
              markDirty(true);
            }}
          />
        )}
        {section === "sandbox" && (
          <SandboxForm
            merged={payload.merged}
            definition={definition}
            onChange={(next) => {
              setDefinition(next);
              markDirty(true);
            }}
          />
        )}
        {section === "channels" && (
          <BindingsForm
            merged={payload.merged}
            definition={definition}
            onChange={(next) => {
              setDefinition(next);
              markDirty(true);
            }}
          />
        )}
        {section === "toolsets" && (
          <ToolsetsForm
            merged={payload.merged}
            definition={definition}
            onChange={(next) => {
              setDefinition(next);
              markDirty(true);
            }}
          />
        )}
        {section === "connector" && (
          <ConnectorForm
            merged={payload.merged}
            definition={definition}
            onChange={(next) => {
              setDefinition(next);
              markDirty(true);
            }}
          />
        )}
        {section === "routing" && routing && routingDraft && (
          <RoutingForm
            routing={routing}
            draft={routingDraft}
            onChange={(next) => {
              setRoutingDraft(next);
              markRoutingDirty(true);
            }}
          />
        )}
        {section === "advanced" && (
          <AdvancedYaml
            draftYaml={payload.redacted_yaml_draft}
            liveYaml={payload.redacted_yaml_live}
          />
        )}
      </div>
    </section>
  );
}
