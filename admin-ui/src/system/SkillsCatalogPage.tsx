import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import type { SkillOrigin } from "../api/types";
import { canManageSkills } from "../auth/session";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { paginate, Pagination } from "../components/Pagination";
import { SkillCreatePanel, type SkillCreateMode } from "./SkillCreatePanel";

type SkillRow = {
  id: string;
  category: string;
  version: string;
  trust_level: string;
  approval_status: string;
  origin?: SkillOrigin;
  description: string;
  agents: string[];
  doctor_ok: boolean;
  used_by_workflows: number;
  used_by_cron: number;
};

type SkillDetail = SkillRow & {
  required_toolsets?: string[];
  required_connectors?: string[];
  source_path?: string;
  files?: string[];
  doctor?: Record<string, string[]>;
  used_by?: {
    workflows: Array<{ workflow_type: string; stage: string }>;
    cron: Array<{ name: string }>;
  };
  inbox_draft_href?: string;
};

type WorkflowSummary = { workflow_type: string; display_name?: string };

export function SkillsCatalogPage({ selectedId }: { selectedId?: string }) {
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [cronJobs, setCronJobs] = useState<Array<{ name: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [workflowType, setWorkflowType] = useState("");
  const [stageName, setStageName] = useState("");
  const [cronName, setCronName] = useState("");
  const [page, setPage] = useState(1);
  const [createMode, setCreateMode] = useState<SkillCreateMode | null>(null);
  const manage = canManageSkills();

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [listed, defs, cron] = await Promise.all([
          apiFetch<{ skills: SkillRow[] }>("/admin/skills"),
          apiFetch<{ definitions: WorkflowSummary[] }>("/admin/workflow-definitions"),
          apiFetch<{ jobs: Array<{ name: string }> }>("/admin/cron/jobs"),
        ]);
        if (cancelled) return;
        setSkills(listed.skills || []);
        setWorkflows(defs.definitions || []);
        setCronJobs(cron.jobs || []);
      } catch (e: any) {
        if (!cancelled) setError(String(e.message || e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    apiFetch<SkillDetail>(`/admin/skills/${encodeURIComponent(selectedId)}`)
      .then((item) => {
        if (!cancelled) setDetail(item);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const selected = useMemo((): SkillDetail | null => {
    if (detail) return detail;
    const row = skills.find((item) => item.id === selectedId);
    return row ? { ...row } : null;
  }, [detail, skills, selectedId]);

  const skillPage = paginate(skills, page);

  function attachWorkflow() {
    if (!selected) return;
    const type = workflowType.trim();
    if (!type) return;
    const stage = stageName.trim();
    const query = new URLSearchParams({ attachSkill: selected.id });
    if (stage) query.set("stage", stage);
    window.location.hash = `#/workflows/${encodeURIComponent(type)}?${query.toString()}`;
  }

  function attachCron(job?: string) {
    if (!selected) return;
    const query = new URLSearchParams({ attachSkill: selected.id });
    const name = (job ?? cronName).trim();
    if (name) query.set("job", name);
    window.location.hash = `#/system/cron?${query.toString()}`;
  }

  function openLearnSkill() {
    window.location.hash = `#/chat?prefill=${encodeURIComponent("/learn ")}`;
  }

  function toggleCreate(mode: SkillCreateMode) {
    setCreateMode((current) => (current === mode ? null : mode));
  }

  const skillsToolbar = (
    <>
      <div className="cron-toolbar">
        <div>
          <h3>Skills</h3>
          <p className="muted">Approved catalog skills and where they are attached.</p>
        </div>
        <div>
          <Button type="button" variant="primary" onClick={openLearnSkill}>
            Learn a skill
          </Button>
          {manage && (
            <>
              <Button type="button" variant="ghost" onClick={() => toggleCreate("paste")}>
                Paste skill
              </Button>
              <Button type="button" variant="ghost" onClick={() => toggleCreate("folder")}>
                Upload folder
              </Button>
            </>
          )}
        </div>
      </div>
      {manage && createMode && (
        <SkillCreatePanel key={createMode} mode={createMode} onClose={() => setCreateMode(null)} />
      )}
    </>
  );

  if (loading && !skills.length) {
    return <PageState kind="loading" title="Loading skills catalog" />;
  }

  return (
    <div className="skills-catalog">
      {error && skills.length > 0 && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {!selectedId ? (
        error && !skills.length ? (
          <PageState kind="error" title="Unable to load skills" detail={error} />
        ) : skills.length ? (
          <>
            {skillsToolbar}
            <table className="cron-table">
              <thead>
                <tr>
                  <th>Skill</th>
                  <th>Category</th>
                  <th>Origin</th>
                  <th>Status</th>
                  <th>Agents</th>
                  <th>Used by</th>
                </tr>
              </thead>
              <tbody>
                {skillPage.items.map((skill) => (
                  <tr key={skill.id}>
                    <td>
                      <a href={`#/system/skills/${encodeURIComponent(skill.id)}`}>{skill.id}</a>
                      <div className="muted">{skill.description}</div>
                    </td>
                    <td>{skill.category}</td>
                    <td>{skill.origin || "builtin"}</td>
                    <td>
                      {skill.approval_status}
                      {skill.doctor_ok ? "" : " · doctor"}
                    </td>
                    <td>{(skill.agents || []).join(", ") || "—"}</td>
                    <td>
                      {skill.used_by_workflows} workflows · {skill.used_by_cron} cron
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination
              page={skillPage.page}
              pageCount={skillPage.pageCount}
              total={skillPage.total}
              start={skillPage.start}
              end={skillPage.end}
              onPageChange={setPage}
              itemLabel="skills"
            />
          </>
        ) : (
          <>
            {skillsToolbar}
            <PageState kind="empty" title="No catalog skills" detail="Approved repo skills will appear here." />
          </>
        )
      ) : selected ? (
        <section className="mcp-detail" aria-label={`${selected.id} detail`}>
          <p>
            <a href="#/system/skills">Back to catalog</a>
          </p>
          <h3>{selected.id}</h3>
          <p className="muted">
            {selected.category} · {selected.version} · {selected.trust_level} · {selected.approval_status} ·{" "}
            {selected.origin || "builtin"}
          </p>
          <p>{selected.description}</p>
          {selected.source_path && <p className="mono">{selected.source_path}</p>}
          {selected.files && selected.files.length > 0 && (
            <>
              <h4>Files</h4>
              <ul aria-label="Skill files" className="mono">
                {selected.files.map((file) => (
                  <li key={file}>{file}</li>
                ))}
              </ul>
            </>
          )}
          {selected.inbox_draft_href && (
            <p>
              <a href={selected.inbox_draft_href}>Open Inbox draft</a>
            </p>
          )}
          <h4>Doctor</h4>
          {Object.keys(selected.doctor || {}).length ? (
            <ul>
              {Object.entries(selected.doctor || {}).map(([agentId, lines]) => (
                <li key={agentId}>
                  {agentId}: {lines.join("; ")}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No doctor skip/block lines.</p>
          )}
          <h4>Used by</h4>
          <ul>
            {(selected.used_by?.workflows || []).map((item) => (
              <li key={`${item.workflow_type}:${item.stage}`}>
                <a
                  href={`#/workflows/${encodeURIComponent(item.workflow_type)}${
                    item.stage ? `?stage=${encodeURIComponent(item.stage)}` : ""
                  }`}
                >
                  {item.workflow_type}
                </a>{" "}
                / {item.stage}
              </li>
            ))}
            {(selected.used_by?.cron || []).map((item) => (
              <li key={item.name}>
                <a href={`#/system/cron?job=${encodeURIComponent(item.name)}`}>{item.name}</a>
              </li>
            ))}
          </ul>
          <section className="mcp-form" aria-label="Attach skill">
            <h4>Attach</h4>
            <label className="form-field">
              <span>Workflow type</span>
              <select
                aria-label="Workflow type"
                value={workflowType}
                onChange={(e) => setWorkflowType(e.target.value)}
              >
                <option value="">Select workflow…</option>
                {workflows.map((item) => (
                  <option key={item.workflow_type} value={item.workflow_type}>
                    {item.display_name || item.workflow_type}
                  </option>
                ))}
              </select>
            </label>
            <label className="form-field">
              <span>Stage (optional)</span>
              <input
                aria-label="Workflow stage"
                value={stageName}
                onChange={(e) => setStageName(e.target.value)}
              />
            </label>
            <Button variant="primary" onClick={attachWorkflow}>
              Attach to workflow
            </Button>
            <label className="form-field">
              <span>Cron job</span>
              <select aria-label="Cron job" value={cronName} onChange={(e) => setCronName(e.target.value)}>
                <option value="">New job</option>
                {cronJobs.map((job) => (
                  <option key={job.name} value={job.name}>
                    {job.name}
                  </option>
                ))}
              </select>
            </label>
            <Button variant="ghost" onClick={() => attachCron()}>
              Attach to cron
            </Button>
          </section>
        </section>
      ) : (
        <PageState kind="empty" title="Skill not found" detail={selectedId} />
      )}
    </div>
  );
}
