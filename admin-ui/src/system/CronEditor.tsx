import React, { useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import type { CronJob, CronProgramConfig } from "../api/types";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageState } from "../components/PageState";
import { pageForIndex, paginate, Pagination } from "../components/Pagination";

const emptyJob: CronJob = {
  name: "",
  agent_id: "ops-agent",
  cron: "0 10 * * 1",
  prompt: "",
  timezone: "UTC",
  active_start: "00:00",
  active_end: "23:59",
};

const START_PROGRAM = "start_program";

function programJob(programType = ""): CronJob {
  return {
    ...emptyJob,
    kind: "system",
    handler: START_PROGRAM,
    program: { program_type: programType, summary: "" },
  };
}

function isProgramJob(job: CronJob | null): boolean {
  return Boolean(job && job.handler === START_PROGRAM);
}

function clearAttachQuery() {
  const raw = window.location.hash.replace(/^#/, "");
  const [path] = raw.split("?");
  if (raw.includes("?")) {
    history.replaceState(null, "", `#${path}`);
  }
}

export function CronEditor({
  attachSkill,
  attachJob,
  attachProgram,
}: {
  attachSkill?: string;
  attachJob?: string;
  attachProgram?: string;
} = {}) {
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [editing, setEditing] = useState<CronJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [programTypes, setProgramTypes] = useState<string[]>([]);
  const [artifactsText, setArtifactsText] = useState("{}");
  const dialogRef = useRef<HTMLDialogElement>(null);
  const attachAppliedRef = useRef<string | null>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  const formTitleId = useId();

  async function load() {
    setLoading(true);
    try {
      const data = await apiFetch<{ jobs: CronJob[] }>("/admin/cron/jobs");
      setJobs(data.jobs || []);
      setError(null);
      setJobsLoaded(true);
    } catch (e: any) {
      setError(String(e.message || e));
      setJobsLoaded(true);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!isProgramJob(editing) || programTypes.length) return;
    apiFetch<{ definitions?: Array<{ program_type: string }> }>("/admin/program-definitions")
      .then((data) =>
        setProgramTypes(
          Array.from(new Set((data.definitions || []).map((item) => item.program_type))).sort()
        )
      )
      .catch(() => setProgramTypes([]));
  }, [editing, programTypes.length]);

  function startEditing(job: CronJob) {
    setArtifactsText(JSON.stringify(job.program?.artifacts || {}, null, 2));
    setEditing(job);
  }

  function updateProgram(patch: Partial<CronProgramConfig>) {
    if (!editing) return;
    const current = editing.program || { program_type: "", summary: "" };
    setEditing({ ...editing, program: { ...current, ...patch } });
  }

  useEffect(() => {
    if (!jobsLoaded || attachSkill || attachJob || !attachProgram) return;
    const token = `program:${attachProgram}`;
    if (attachAppliedRef.current === token) return;
    attachAppliedRef.current = token;
    startEditing(programJob(attachProgram));
    clearAttachQuery();
  }, [attachProgram, attachSkill, attachJob, jobsLoaded]);

  useEffect(() => {
    if (!jobsLoaded || (!attachSkill && !attachJob)) return;
    const token = `${attachSkill || ""}:${attachJob || ""}`;
    if (attachAppliedRef.current === token) return;
    attachAppliedRef.current = token;
    if (attachJob) {
      const jobIndex = jobs.findIndex((item) => item.name === attachJob);
      const job = jobIndex >= 0 ? jobs[jobIndex] : undefined;
      if (!job) {
        setError(`Unknown cron job: ${attachJob}`);
        clearAttachQuery();
        return;
      }
      setPage(pageForIndex(jobIndex));
      startEditing(attachSkill ? { ...job, skill_id: attachSkill } : { ...job });
    } else if (attachSkill) {
      startEditing({ ...emptyJob, skill_id: attachSkill });
    }
    clearAttachQuery();
  }, [attachSkill, attachJob, jobs, jobsLoaded]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (editing && !dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
      firstFieldRef.current?.focus();
    } else if (!editing && dialog.open) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [editing]);

  async function save() {
    if (!editing) return;
    setError(null);
    setSuccess(null);
    let body: Record<string, unknown>;
    if (isProgramJob(editing)) {
      let artifacts: Record<string, unknown> = {};
      try {
        const parsed = JSON.parse(artifactsText.trim() || "{}");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
        artifacts = parsed;
      } catch {
        setError("Artifacts must be a JSON object");
        return;
      }
      const program = editing.program || { program_type: "", summary: "" };
      const notify = program.notify?.channel?.trim()
        ? {
            channel: program.notify.channel.trim(),
            ...(program.notify.text?.trim() ? { text: program.notify.text.trim() } : {}),
          }
        : undefined;
      body = {
        name: editing.name,
        kind: "system",
        handler: START_PROGRAM,
        agent_id: editing.agent_id || "ops-agent",
        cron: editing.cron,
        timezone: editing.timezone,
        active_start: editing.active_start,
        active_end: editing.active_end,
        program: {
          program_type: program.program_type,
          summary: program.summary,
          ...(program.entry_node?.trim() ? { entry_node: program.entry_node.trim() } : {}),
          ...(Object.keys(artifacts).length ? { artifacts } : {}),
          ...(notify ? { notify } : {}),
        },
      };
    } else {
      body = {
        ...editing,
        skill_hints: editing.skill_id
          ? [editing.skill_id, ...(editing.skill_hints || []).filter((h) => h !== editing.skill_id)]
          : editing.skill_hints,
      };
    }
    setBusy("save");
    try {
      const exists = jobs.some((j) => j.name === editing.name);
      if (exists) {
        await apiFetch(`/admin/cron/jobs/${encodeURIComponent(editing.name)}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      } else {
        await apiFetch("/admin/cron/jobs", { method: "POST", body: JSON.stringify(body) });
      }
      setEditing(null);
      await load();
      setSuccess(`Saved ${editing.name}`);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  async function remove(name: string) {
    setError(null);
    setSuccess(null);
    setBusy(`delete:${name}`);
    try {
      await apiFetch(`/admin/cron/jobs/${encodeURIComponent(name)}`, { method: "DELETE" });
      await load();
      setSuccess(`Deleted ${name}`);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setPendingDelete(null);
      setBusy(null);
    }
  }

  async function trigger(name: string) {
    setError(null);
    setSuccess(null);
    setBusy(`trigger:${name}`);
    try {
      await apiFetch(`/admin/cron/jobs/${encodeURIComponent(name)}/trigger`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      await load();
      setSuccess(`Triggered ${name}`);
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(null);
    }
  }

  const isEditingExisting = Boolean(editing && jobs.some((j) => j.name === editing.name));
  const jobPage = paginate(jobs, page);

  return (
    <div className="cron-editor">
      <div className="cron-toolbar">
        <div>
          <h3>Cron jobs</h3>
          <p className="muted">Create, update, trigger, and delete file-backed schedules.</p>
        </div>
        <Button
          variant="primary"
          onClick={() => {
            setError(null);
            setSuccess(null);
            startEditing({ ...emptyJob });
          }}
        >
          New job
        </Button>
        <Button variant="ghost" busy={loading && jobs.length > 0} onClick={() => void load()}>
          Refresh
        </Button>
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {success && (
        <div className="success" role="status">
          {success}
        </div>
      )}
      <div className="cron-table-wrap">
        {loading && !jobs.length ? (
          <PageState kind="loading" title="Loading cron jobs" />
        ) : jobs.length ? (
          <>
          <table className="cron-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Schedule</th>
                <th>Agent</th>
                <th>Skill</th>
                <th>Next</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobPage.items.map((j) => (
                <tr key={j.name}>
                  <td className="mono">{j.name}</td>
                  <td className="mono">{j.cron || `${j.interval_seconds}s`}</td>
                  <td>{isProgramJob(j) ? `program: ${j.program?.program_type || "-"}` : j.agent_id}</td>
                  <td className="mono">{j.skill_id || (j.skill_hints || []).join(", ") || "-"}</td>
                  <td className="muted">{j.next_run || "-"}</td>
                  <td>
                    <div className="row-actions">
                      <Button
                        variant="ghost"
                        onClick={() => {
                          setError(null);
                          setSuccess(null);
                          startEditing({ ...j });
                        }}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="ghost"
                        busy={busy === `trigger:${j.name}`}
                        onClick={() => void trigger(j.name)}
                      >
                        Trigger
                      </Button>
                      <Button
                        variant="danger"
                        busy={busy === `delete:${j.name}`}
                        onClick={() => setPendingDelete(j.name)}
                      >
                        Delete
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination
            page={jobPage.page}
            pageCount={jobPage.pageCount}
            total={jobPage.total}
            start={jobPage.start}
            end={jobPage.end}
            onPageChange={setPage}
            itemLabel="jobs"
          />
          </>
        ) : (
          <PageState
            kind="empty"
            title="No file-backed cron jobs"
            detail="Create a job to schedule recurring operator work."
          />
        )}
      </div>

      <dialog
        ref={dialogRef}
        aria-labelledby={formTitleId}
        className="dialog dialog-form"
        onCancel={(event) => {
          event.preventDefault();
          setEditing(null);
        }}
      >
        {editing && (
          <section className="cron-form" aria-label="Cron job form">
            <h2 id={formTitleId}>{isEditingExisting ? "Edit" : "Create"} cron job</h2>
            <label className="form-field">
              <span>Job type</span>
              <select
                aria-label="Job type"
                value={isProgramJob(editing) ? "program" : "prompt"}
                disabled={isEditingExisting}
                onChange={(e) => {
                  const base = { name: editing.name, cron: editing.cron, timezone: editing.timezone };
                  startEditing(
                    e.target.value === "program"
                      ? { ...programJob(), ...base }
                      : { ...emptyJob, ...base }
                  );
                }}
              >
                <option value="prompt">Agent prompt</option>
                <option value="program">Start program</option>
              </select>
            </label>
            {(
              (isProgramJob(editing)
                ? [
                    ["name", "Name"],
                    ["cron", "Cron expression"],
                    ["timezone", "Timezone"],
                    ["active_start", "Active start"],
                    ["active_end", "Active end"],
                  ]
                : [
                    ["name", "Name"],
                    ["agent_id", "Agent"],
                    ["cron", "Cron expression"],
                    ["timezone", "Timezone"],
                    ["skill_id", "Skill id"],
                    ["prompt", "Prompt"],
                    ["active_start", "Active start"],
                    ["active_end", "Active end"],
                  ]) as ReadonlyArray<readonly [keyof CronJob, string]>
            ).map(([key, label], index) => (
              <label key={key} className="form-field">
                <span>{label}</span>
                <input
                  ref={index === 0 ? firstFieldRef : undefined}
                  aria-label={label}
                  value={String((editing as any)[key] || "")}
                  disabled={key === "name" && isEditingExisting}
                  onChange={(e) => setEditing({ ...editing, [key]: e.target.value })}
                />
              </label>
            ))}
            {isProgramJob(editing) && (
              <>
                <label className="form-field">
                  <span>Program type</span>
                  <select
                    aria-label="Program type"
                    value={editing.program?.program_type || ""}
                    onChange={(e) => updateProgram({ program_type: e.target.value })}
                  >
                    <option value="">Select a program</option>
                    {Array.from(
                      new Set([...programTypes, editing.program?.program_type || ""].filter(Boolean))
                    ).map((type) => (
                      <option key={type} value={type}>
                        {type}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="form-field">
                  <span>Entry node</span>
                  <input
                    aria-label="Entry node"
                    placeholder="First startable node"
                    value={editing.program?.entry_node || ""}
                    onChange={(e) => updateProgram({ entry_node: e.target.value })}
                  />
                </label>
                <label className="form-field">
                  <span>Run summary</span>
                  <input
                    aria-label="Run summary"
                    value={editing.program?.summary || ""}
                    onChange={(e) => updateProgram({ summary: e.target.value })}
                  />
                </label>
                <label className="form-field">
                  <span>Artifacts (JSON)</span>
                  <textarea
                    aria-label="Artifacts (JSON)"
                    rows={4}
                    className="mono"
                    value={artifactsText}
                    onChange={(e) => setArtifactsText(e.target.value)}
                  />
                </label>
                <label className="form-field">
                  <span>Post to Slack channel</span>
                  <input
                    aria-label="Post to Slack channel"
                    placeholder="Channel id, e.g. C0123 (optional)"
                    value={editing.program?.notify?.channel || ""}
                    onChange={(e) =>
                      updateProgram({
                        notify: { ...(editing.program?.notify || {}), channel: e.target.value },
                      })
                    }
                  />
                </label>
                <label className="form-field">
                  <span>Start message</span>
                  <input
                    aria-label="Start message"
                    placeholder="Scheduled run: <summary>"
                    value={editing.program?.notify?.text || ""}
                    onChange={(e) =>
                      updateProgram({
                        notify: {
                          channel: editing.program?.notify?.channel || "",
                          text: e.target.value,
                        },
                      })
                    }
                  />
                </label>
                <p className="muted">
                  A tick is skipped while the previous run of this job is still open, including runs
                  waiting for approval in Inbox.
                </p>
              </>
            )}
            {error && <p className="field-error">{error}</p>}
            <div className="dialog-actions">
              <Button variant="ghost" disabled={busy === "save"} onClick={() => setEditing(null)}>
                Cancel
              </Button>
              <Button variant="primary" busy={busy === "save"} onClick={() => void save()}>
                Save
              </Button>
            </div>
          </section>
        )}
      </dialog>
      <ConfirmDialog
        open={Boolean(pendingDelete)}
        title="Delete cron job?"
        description={`This removes ${pendingDelete || "the selected cron job"} from the file-backed schedule.`}
        confirmLabel="Delete job"
        danger
        busy={Boolean(pendingDelete && busy === `delete:${pendingDelete}`)}
        onConfirm={() => pendingDelete && void remove(pendingDelete)}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
