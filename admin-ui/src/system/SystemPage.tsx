import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { normalizeHealth } from "../api/normalizers";
import type { HealthReport, HealthStatus, NormalizedHealthReport } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { StatusPill } from "../components/StatusPill";
import { Tabs, tabButtonId, tabPanelId } from "../components/Tabs";
import { CronEditor } from "./CronEditor";
import { AgentsPage } from "./AgentsPage";
import { McpPage } from "./McpPage";
import { ProjectSettingsPage } from "../projects/ProjectSettingsPage";
import { SettingsPage } from "./SettingsPage";
import { SkillsCatalogPage } from "./SkillsCatalogPage";
import type { SettingsSection } from "./settings/types";

const TABS_ID = "system-sections";

function formatLastUpdated(date: Date | null): string {
  if (!date) return "Not refreshed yet";
  if (Date.now() - date.getTime() < 60_000) return "Updated just now";
  return `Updated ${date.toLocaleTimeString()}`;
}

function statusIcon(status: HealthStatus): string {
  if (status === "ok") return "OK";
  if (status === "degraded") return "!";
  if (status === "not_configured") return "–";
  return "X";
}

export function SystemPage({
  section,
  settingsSection,
  mcpName,
  agentId,
  skillId,
  projectId,
  attachSkill,
  attachJob,
  attachProgram,
  onSettingsDirtyChange,
  onMcpDirtyChange,
  onAgentsDirtyChange,
}: {
  section?: string;
  settingsSection?: SettingsSection;
  mcpName?: string;
  agentId?: string;
  skillId?: string;
  projectId?: string;
  attachSkill?: string;
  attachJob?: string;
  attachProgram?: string;
  onSettingsDirtyChange?: (dirty: boolean) => void;
  onMcpDirtyChange?: (dirty: boolean) => void;
  onAgentsDirtyChange?: (dirty: boolean) => void;
}) {
  const [health, setHealth] = useState<NormalizedHealthReport | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [staleWarning, setStaleWarning] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const healthRef = useRef<NormalizedHealthReport | null>(null);
  const activeSection =
    section === "cron"
      ? "cron"
      : section === "settings"
        ? "settings"
        : section === "skills"
          ? "skills"
          : section === "mcp"
            ? "mcp"
            : section === "agents"
              ? "agents"
              : section === "projects"
                ? "projects"
                : "health";

  const load = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (!background) setLoading(true);
      try {
        const report = normalizeHealth(await apiFetch<HealthReport>("/healthz"));
        healthRef.current = report;
        setHealth(report);
        setLastUpdated(new Date());
        setError(null);
        setStaleWarning(null);
      } catch (e: any) {
        const message = String(e.message || e);
        if (background && healthRef.current) {
          setStaleWarning(message);
        } else {
          setError(message);
        }
      } finally {
        if (!background) setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    if (activeSection === "cron" || activeSection === "settings" || activeSection === "skills" || activeSection === "mcp" || activeSection === "agents" || activeSection === "projects") return;
    void load();
    const id = window.setInterval(() => void load({ background: true }), 30_000);
    return () => window.clearInterval(id);
  }, [activeSection, load]);

  function healthContent() {
    if (loading && !health) {
      return <PageState kind="loading" title="Loading system health" />;
    }
    if (error && !health) {
      return (
        <PageState
          kind="error"
          title="Unable to load system health"
          detail={error}
          onRetry={() => void load()}
        />
      );
    }
    if (!health) {
      return (
        <PageState kind="empty" title="No health data" detail="Refresh to check runtime health." />
      );
    }

    return (
      <section className="system-health" aria-label="System health">
        {staleWarning && (
          <PageState
            kind="stale"
            title="Health data may be stale"
            detail={staleWarning}
            onRetry={() => void load()}
          />
        )}
        <div className="system-summary">
          <div>
            <p className="eyebrow">Overall status</p>
            <h3>
              <span className="status-icon" aria-hidden="true">
                {statusIcon(health.status)}
              </span>
              System health
            </h3>
            <p className="muted">
              {formatLastUpdated(lastUpdated)}
              {health.version ? ` · Version ${health.version}` : ""}
            </p>
          </div>
          <StatusPill status={health.status} />
        </div>
        <div className="health-grid">
          {health.components.map((component) => (
            <article
              key={component.name}
              className={`health-card health-card-${component.status}`}
            >
              <div className="health-card-header">
                <div>
                  <p className="eyebrow">Component</p>
                  <h4>{component.label}</h4>
                </div>
                <span className="status-icon" aria-hidden="true">
                  {statusIcon(component.status)}
                </span>
              </div>
              <StatusPill status={component.status} />
              {component.detail && <p className="muted">{component.detail}</p>}
            </article>
          ))}
        </div>
      </section>
    );
  }

  return (
    <div className="panel-pad system-page">
      <div className="system-header">
        <div>
          <h2>System</h2>
          <p className="muted">Runtime health, scheduled operations, skills, MCP, projects, and tenant settings.</p>
        </div>
        {activeSection === "health" && (
          <Button variant="ghost" busy={loading && Boolean(health)} onClick={() => void load()}>
            Refresh
          </Button>
        )}
      </div>
      <Tabs
        idBase={TABS_ID}
        ariaLabel="System sections"
        active={activeSection}
        tabs={[
          { id: "health", label: "Health" },
          { id: "cron", label: "Cron" },
          { id: "skills", label: "Skills" },
          { id: "mcp", label: "MCP" },
          { id: "agents", label: "Agents" },
          { id: "projects", label: "Projects" },
          { id: "settings", label: "Settings" },
        ]}
        onChange={(id) => {
          window.location.hash =
            id === "cron"
              ? "#/system/cron"
              : id === "settings"
                ? "#/system/settings"
                : id === "skills"
                  ? "#/system/skills"
                  : id === "mcp"
                    ? "#/system/mcp"
                    : id === "agents"
                      ? "#/system/agents"
                      : id === "projects"
                        ? "#/system/projects"
                        : "#/system";
        }}
      />
      <div
        role="tabpanel"
        id={tabPanelId(TABS_ID, activeSection)}
        aria-labelledby={tabButtonId(TABS_ID, activeSection)}
        tabIndex={0}
      >
        {activeSection === "cron" ? (
          <CronEditor attachSkill={attachSkill} attachJob={attachJob} attachProgram={attachProgram} />
        ) : activeSection === "skills" ? (
          <SkillsCatalogPage selectedId={skillId} />
        ) : activeSection === "mcp" ? (
          <McpPage selectedName={mcpName} onDirtyChange={onMcpDirtyChange} />
        ) : activeSection === "agents" ? (
          <AgentsPage selectedId={agentId} onDirtyChange={onAgentsDirtyChange} />
        ) : activeSection === "projects" ? (
          <ProjectSettingsPage selectedId={projectId} />
        ) : activeSection === "settings" ? (
          <SettingsPage section={settingsSection} onDirtyChange={onSettingsDirtyChange} />
        ) : (
          healthContent()
        )}
      </div>
    </div>
  );
}
