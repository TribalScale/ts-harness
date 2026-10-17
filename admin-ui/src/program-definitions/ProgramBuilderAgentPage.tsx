import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { ChatThread } from "../chat/ChatThread";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { PlusIcon } from "../components/PlusIcon";
import type { ProgramRoute, ValidationIssue } from "./types";

export type BuilderPartKind = "workflow" | "program" | "routing";

export interface BuilderPartState {
  status: "draft" | "missing" | "activated" | "none" | string;
  draft_id?: string | null;
  revision?: string | null;
  activated?: Record<string, unknown> | null;
  issues: ValidationIssue[];
  valid: boolean;
  workflow_type?: string | null;
  program_type?: string | null;
  intent_id?: string | null;
  signals?: string[];
  blocked?: string;
}

export interface BuilderSessionState {
  session_id: string;
  workflow: BuilderPartState;
  program: BuilderPartState;
  routing: BuilderPartState;
  skills: Array<{ skill_id: string; status: string }>;
  problems: string[];
  ready: boolean;
}

export interface BuilderActivateStep {
  kind: BuilderPartKind;
  status: "activated" | "already_active" | "skipped" | "failed" | "not_run" | string;
  error?: string;
}

interface BuilderSessionSummary {
  session_id: string;
  started_at?: string;
  workflow_type?: string | null;
  program_type?: string | null;
}

interface Intake {
  name: string;
  purpose: string;
  stages: string;
  channels: string;
  trigger_phrases: string;
}

const EMPTY_INTAKE: Intake = { name: "", purpose: "", stages: "", channels: "", trigger_phrases: "" };

const PARTS: BuilderPartKind[] = ["workflow", "program", "routing"];

const PART_LABELS: Record<BuilderPartKind, string> = {
  workflow: "Workflow",
  program: "Program",
  routing: "Routing intent",
};

const STEP_LABELS: Record<string, string> = {
  activated: "Activated",
  already_active: "Already active",
  skipped: "Nothing to activate",
  failed: "Failed",
  not_run: "Not run",
};

const SAMPLE_PROMPTS = [
  "Draft the program from my intake",
  "Which skills are missing?",
  "Where are the approval gates?",
];

const POLL_MS = 5000;

function splitList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function intakeBody(intake: Intake): Record<string, unknown> | undefined {
  const body = {
    name: intake.name.trim(),
    purpose: intake.purpose.trim(),
    stages: intake.stages.trim(),
    channels: splitList(intake.channels),
    trigger_phrases: splitList(intake.trigger_phrases),
  };
  const filled = Object.values(body).some((value) => (Array.isArray(value) ? value.length : value));
  return filled ? body : undefined;
}

function draftHref(kind: BuilderPartKind, draftId: string): string {
  if (kind === "workflow") return `#/workflows/drafts/${encodeURIComponent(draftId)}`;
  if (kind === "program") return `#/workflows/programs/drafts/${encodeURIComponent(draftId)}`;
  return "#/system/settings/routing";
}

function partStatusText(part: BuilderPartState): string {
  if (part.status === "draft") return "Draft";
  if (part.status === "activated") return "Active";
  if (part.status === "missing") return "Discarded";
  return "Not drafted yet";
}

function countIssues(issues: ValidationIssue[]) {
  return {
    errors: issues.filter((issue) => issue.severity === "error").length,
    warnings: issues.filter((issue) => issue.severity === "warning").length,
  };
}

function DraftCard({ kind, part }: { kind: BuilderPartKind; part: BuilderPartState }) {
  const { errors, warnings } = countIssues(part.issues);
  return (
    <section className="builder-card" aria-label={`${PART_LABELS[kind]} draft`}>
      <header className="builder-card-header">
        <h3>{PART_LABELS[kind]}</h3>
        <span className={`badge builder-status-${part.status}`}>{partStatusText(part)}</span>
      </header>
      {part.status === "missing" && (
        <p className="muted">Discarded. Ask the agent to recreate it.</p>
      )}
      {part.blocked && <p className="field-error">{part.blocked}</p>}
      {part.status === "draft" && part.draft_id && (
        <p className="muted">
          {errors} {errors === 1 ? "error" : "errors"}, {warnings} {warnings === 1 ? "warning" : "warnings"}
          {" · "}
          <a href={draftHref(kind, part.draft_id)}>Open in builder</a>
        </p>
      )}
      {part.issues.length > 0 && (
        <ul className="builder-issues">
          {part.issues.map((issue, index) => (
            <li key={`${issue.code}-${issue.path}-${index}`} className={`builder-issue-${issue.severity}`}>
              <strong>{issue.severity}</strong> {issue.message}
              {issue.path && <span className="muted"> ({issue.path})</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SkillsCard({ skills }: { skills: BuilderSessionState["skills"] }) {
  if (!skills.length) return null;
  return (
    <section className="builder-card" aria-label="Proposed skills">
      <header className="builder-card-header">
        <h3>Skills</h3>
        <a href="#/inbox/skill_draft">Review in Inbox</a>
      </header>
      <ul className="builder-issues">
        {skills.map((skill) => (
          <li key={skill.skill_id}>
            <code>{skill.skill_id}</code>{" "}
            <span className={`badge builder-status-${skill.status}`}>{skill.status}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function expectedRevisions(state: BuilderSessionState): Record<string, string> {
  const revisions: Record<string, string> = {};
  for (const kind of PARTS) {
    const revision = state[kind].revision;
    if (state[kind].status === "draft" && revision) revisions[kind] = revision;
  }
  return revisions;
}

export function ProgramBuilderAgentPage({
  sessionId,
  onNavigate,
}: {
  sessionId?: string;
  onNavigate: (route: ProgramRoute) => void;
}) {
  const [intake, setIntake] = useState<Intake>(EMPTY_INTAKE);
  const [chatError, setChatError] = useState<string | null>(null);
  const [state, setState] = useState<BuilderSessionState | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<BuilderSessionSummary[]>([]);
  const [steps, setSteps] = useState<BuilderActivateStep[] | null>(null);
  const [activating, setActivating] = useState(false);
  const [activateError, setActivateError] = useState<string | null>(null);
  const [activatedProgram, setActivatedProgram] = useState<string | null>(null);

  const loadSessions = useCallback(async () => {
    try {
      const data = await apiFetch<{ sessions?: BuilderSessionSummary[] }>("/admin/program-builder/sessions");
      setSessions(data.sessions || []);
    } catch {
      setSessions([]);
    }
  }, []);

  const loadState = useCallback(async (id: string) => {
    try {
      const data = await apiFetch<BuilderSessionState>(
        `/admin/program-builder/sessions/${encodeURIComponent(id)}`
      );
      setState(data);
      setStateError(null);
    } catch (e: any) {
      setStateError(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions, sessionId]);

  useEffect(() => {
    setState(null);
    setStateError(null);
    setSteps(null);
    setActivateError(null);
    setActivatedProgram(null);
    if (!sessionId) {
      setIntake(EMPTY_INTAKE);
      setChatError(null);
      return;
    }
    void loadState(sessionId);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void loadState(sessionId);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadState, sessionId]);

  const extraBody = useMemo(() => {
    if (sessionId) return {};
    const body = intakeBody(intake);
    return body ? { intake: body } : {};
  }, [intake, sessionId]);

  const blockingErrors = useMemo(() => {
    if (!state) return 0;
    return PARTS.reduce((total, kind) => total + countIssues(state[kind].issues).errors, 0);
  }, [state]);

  const hasDrafts = Boolean(state && PARTS.some((kind) => state[kind].status === "draft"));
  const canActivate = Boolean(state?.ready) && hasDrafts && !activating;
  const failedStep = steps?.find((step) => step.status === "failed");

  async function activateAll() {
    if (!sessionId || !state) return;
    setActivating(true);
    setActivateError(null);
    try {
      const result = await apiFetch<{
        ok: boolean;
        steps: BuilderActivateStep[];
        state: BuilderSessionState;
      }>(`/admin/program-builder/sessions/${encodeURIComponent(sessionId)}/activate`, {
        method: "POST",
        body: JSON.stringify({ expected_revisions: expectedRevisions(state) }),
      });
      setSteps(result.steps);
      setState(result.state);
      if (result.ok) {
        setActivatedProgram(result.state.program.program_type || null);
      }
      void loadSessions();
    } catch (e: any) {
      setActivateError(String(e.message || e));
      void loadState(sessionId);
    } finally {
      setActivating(false);
    }
  }

  const triggerPhrases = state?.routing.signals?.length
    ? state.routing.signals
    : splitList(intake.trigger_phrases);

  return (
    <div className="program-builder-agent">
      <section className="program-builder-chat" aria-label="Builder chat">
        <div className="chat-toolbar">
          <Button variant="ghost" size="sm" type="button" onClick={() => onNavigate({ kind: "library" })}>
            Back to programs
          </Button>
          <label className="program-builder-session-picker">
            <span className="muted">Earlier builds</span>
            <select
              aria-label="Earlier builds"
              value={sessionId || ""}
              onChange={(event) => {
                const next = event.target.value;
                onNavigate(next ? { kind: "build", sessionId: next } : { kind: "build" });
              }}
            >
              <option value="">New build</option>
              {sessions.map((item) => (
                <option key={item.session_id} value={item.session_id}>
                  {item.program_type || item.workflow_type || "Untitled"} · {item.session_id.slice(0, 8)}
                </option>
              ))}
            </select>
          </label>
          {sessionId && (
            <Button
              variant="primary"
              size="sm"
              type="button"
              className="chat-new-button"
              icon={<PlusIcon />}
              onClick={() => onNavigate({ kind: "build" })}
            >
              New build
            </Button>
          )}
        </div>
        {!sessionId && (
          <form
            className="program-builder-intake"
            aria-label="Program intake"
            onSubmit={(event) => event.preventDefault()}
          >
            <p className="muted">
              Describe the program, then send a message. The agent drafts a workflow, a program, and a
              routing intent as you. Nothing goes live until you click Activate all.
            </p>
            <label className="form-field">
              <span>Program name</span>
              <input
                aria-label="Program name"
                value={intake.name}
                onChange={(e) => setIntake({ ...intake, name: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span>Purpose</span>
              <textarea
                aria-label="Purpose"
                rows={2}
                value={intake.purpose}
                onChange={(e) => setIntake({ ...intake, purpose: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span>Stages, and who approves what</span>
              <textarea
                aria-label="Stages"
                rows={3}
                value={intake.stages}
                onChange={(e) => setIntake({ ...intake, stages: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span>Channels it posts to (comma separated)</span>
              <input
                aria-label="Channels"
                value={intake.channels}
                onChange={(e) => setIntake({ ...intake, channels: e.target.value })}
              />
            </label>
            <label className="form-field">
              <span>Trigger phrases (comma separated)</span>
              <input
                aria-label="Trigger phrases"
                value={intake.trigger_phrases}
                onChange={(e) => setIntake({ ...intake, trigger_phrases: e.target.value })}
              />
            </label>
          </form>
        )}
        <ChatThread
          sessionId={sessionId}
          sessions={[]}
          bindingKey="program-builder"
          error={chatError}
          endpoint="/admin/program-builder/message"
          extraBody={extraBody}
          agentId="builder-agent"
          samplePrompts={SAMPLE_PROMPTS}
          placeholder="Message the program builder…"
          emptyTitle="Build a program with the agent"
          emptyDetail="It drafts the workflow, program, and routing intent, and proposes any missing skills."
          onSelectSession={(id) => onNavigate({ kind: "build", sessionId: id })}
          onSessionsChanged={async () => {
            await loadSessions();
            if (sessionId) await loadState(sessionId);
          }}
          onDetailHeld={() => undefined}
          onError={setChatError}
        />
      </section>
      <aside className="program-builder-drafts" aria-label="Drafts">
        <h2>Drafts</h2>
        {!sessionId && <p className="muted">Drafts appear here once the agent writes them.</p>}
        {sessionId && !state && !stateError && <PageState kind="loading" title="Loading drafts" />}
        {stateError && (
          <PageState
            kind="error"
            title="Could not load drafts"
            detail={stateError}
            onRetry={() => sessionId && void loadState(sessionId)}
          />
        )}
        {state && (
          <>
            {PARTS.map((kind) => (
              <DraftCard key={kind} kind={kind} part={state[kind]} />
            ))}
            <SkillsCard skills={state.skills} />
            {state.problems.length > 0 && hasDrafts && (
              <ul className="builder-problems" aria-label="Before you can activate">
                {state.problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            )}
            <div className="program-builder-activate">
              <Button
                variant="primary"
                type="button"
                busy={activating}
                disabled={!canActivate}
                onClick={() => void activateAll()}
              >
                {failedStep ? "Retry remaining" : "Activate all"}
              </Button>
              {blockingErrors > 0 && (
                <p className="muted">
                  Fix {blockingErrors} {blockingErrors === 1 ? "error" : "errors"} before activating.
                </p>
              )}
            </div>
            {activateError && (
              <div className="error" role="alert">
                {activateError}
              </div>
            )}
            {steps && (
              <ol className="builder-steps" aria-label="Activation steps">
                {steps.map((step) => (
                  <li key={step.kind} className={`builder-step-${step.status}`}>
                    <strong>{PART_LABELS[step.kind]}</strong>: {STEP_LABELS[step.status] || step.status}
                    {step.error && <span className="field-error"> {step.error}</span>}
                  </li>
                ))}
              </ol>
            )}
            {activatedProgram && (
              <div className="builder-success" role="status">
                <p>
                  Program is live.{" "}
                  <a href={`#/workflows/programs/${encodeURIComponent(activatedProgram)}`}>Open the program</a>
                </p>
                {triggerPhrases.length > 0 && (
                  <p className="muted">
                    Start it from a channel by saying: {triggerPhrases.map((phrase) => `"${phrase}"`).join(", ")}
                  </p>
                )}
              </div>
            )}
          </>
        )}
      </aside>
    </div>
  );
}
