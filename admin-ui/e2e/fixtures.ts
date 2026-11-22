/**
 * Gateway responses for the browser tests, in the shapes the real API returns
 * (see src/api/types.ts and runtime/ts_harness/gateway/admin_bff.py, workflows/live_view.py).
 * The sample project is a small "Bookshelf" app tracked in Jira project DEMO.
 */
import type {
  AuthConfig,
  InboxItem,
  LiveResponse,
  TicketStoryResponse,
} from "../src/api/types";
import type { RuntimeSettingsGet } from "../src/system/settings/types";

export const PROGRAM_ID = "prog-bookshelf-0001";
export const REPO = "example-org/bookshelf";

function ago(seconds: number): string {
  return new Date(Date.now() - seconds * 1000).toISOString();
}

export const authConfig: AuthConfig = {
  mode: "static",
  authenticated: true,
  roles: ["admin"],
  permissions: ["view", "edit_config", "approve_gate", "start_program"],
};

export const gateItem: InboxItem = {
  type: "workflow_gate",
  id: "task-gate-prd-1",
  title: "approve_prd",
  subtitle: "Program Bookshelf: reading list and loans",
  created_at: ago(600),
  href: "/#/inbox/workflow_gate/task-gate-prd-1",
  meta: {
    program_id: PROGRAM_ID,
    task_id: "task-gate-prd-1",
    step_name: "approve_prd",
    waiting_on: { kind: "approval", match: { task_id: "task-gate-prd-1", step_name: "approve_prd" }, since: ago(600) },
    recommended_action: "Review the PRD draft and approve it or ask for changes.",
    prd_checks: [],
  },
};

export const stuckItem: InboxItem = {
  type: "stuck_step",
  id: "task-finalize-7",
  title: "DEMO-12 · finalize_code_review",
  subtitle: "Revision limit reached · waiting for your decision",
  created_at: ago(1200),
  href: "/#/inbox/stuck_step/task-finalize-7",
  meta: {
    workflow_id: "wf-demo-12",
    task_id: "task-finalize-7",
    step_name: "finalize_code_review",
    work_order: "DEMO-12",
    reason: "revision_limit",
    decisions: ["revise", "accept", "stop"],
    feedback: "The loan due-date calculation ignores the library's time zone.",
    pr_url: `https://github.com/${REPO}/pull/41`,
    program_id: PROGRAM_ID,
    status: "blocked",
    updated_at: ago(1200),
  },
};

export const proposalItem: InboxItem = {
  type: "pending_proposal",
  id: "sess-web-77:pending_program",
  title: "Start a program `jira_to_pr`: implement all DEMO",
  subtitle: "Waiting for a yes or no in web",
  created_at: ago(1800),
  href: "/#/inbox/pending_proposal/sess-web-77%3Apending_program",
  meta: {
    session_id: "sess-web-77",
    key: "pending_program",
    description: "Start a program `jira_to_pr`: implement all DEMO",
    source: "web",
    proposed_at: ago(1800),
    status: "pending",
  },
};

export function inboxItems(): InboxItem[] {
  return [gateItem, stuckItem, proposalItem];
}

export const LIVE_WORKFLOW_ID = "wf-demo-14";

export function liveResponse(): LiveResponse {
  return {
    generated_at: new Date().toISOString(),
    capacity: { running: 1, queued: 0, limit: 4 },
    items: [
      {
        workflow_id: LIVE_WORKFLOW_ID,
        workflow_type: "implement_jira_ticket",
        label: "Add a due-date reminder to loans",
        state: "OpenCode running",
        kind: "running",
        current_step: "implement",
        revisions: 1,
        pr_url: `https://github.com/${REPO}/pull/44`,
        stuck_task_id: "",
        coding_job: {
          id: "job-901",
          status: "running",
          last_output: "vitest: 42 passed",
          started_at: ago(430),
          created_at: ago(440),
          model: "claude-opus-5-5",
          progress: {
            phase: "verification",
            phase_seconds: 40,
            detail: "npm test",
            done: { setup: 35, coding: 355 },
          },
        },
        jira_issue_key: "DEMO-14",
        waiting_on: "agent",
        in_step_since: ago(430),
        in_step_seconds: 430,
        started_at: ago(2400),
        elapsed_seconds: 2400,
        stronger_model: false,
        timeline: [
          {
            task_id: "t-1",
            step_name: "implement",
            label: "Coding",
            category: "coding",
            status: "coding",
            run: 2,
            start: ago(430),
            end: new Date().toISOString(),
            seconds: 430,
            open: true,
          },
        ],
        program_id: PROGRAM_ID,
        program_label: "Bookshelf: reading list and loans",
      },
    ],
  };
}

export function ticketStory(): TicketStoryResponse {
  const findings = [
    { severity: "high", category: "correctness", file: "src/loans/dueDate.ts", line: 18, message: "Due date ignores the library time zone." },
    { severity: "medium", category: "tests", file: "src/loans/dueDate.test.ts", line: 4, message: "No test for a loan that crosses a daylight-saving change." },
    { severity: "medium", category: "api", file: "src/api/loans.ts", line: 52, message: "Reminder endpoint returns 200 for an unknown loan id." },
    { severity: "low", category: "style", file: "src/loans/Reminder.tsx", line: 9, message: "Reminder copy says 'book' for every item type." },
  ];
  return {
    workflow_id: LIVE_WORKFLOW_ID,
    generated_at: new Date().toISOString(),
    started_at: ago(2400),
    cost_usd: 1.84,
    totals: [
      { category: "coding", label: "Coding", seconds: 1500, share: 0.62 },
      { category: "review", label: "Code review", seconds: 600, share: 0.25 },
      { category: "ci", label: "CI checks", seconds: 300, share: 0.13 },
    ],
    current_run: {
      title: "Revision 1",
      model: "Opus",
      started_at: ago(430),
      seconds: 430,
      queued: false,
      phase: "verification",
      phase_seconds: 40,
      detail: "npm test",
      done: { setup: 35, coding: 355 },
    },
    events: [
      {
        at: ago(2400),
        kind: "coding",
        title: "First attempt",
        status: "ok",
        seconds: 900,
        model: "Opus",
        cost_usd: 0.92,
        phases: { setup: 40, coding: 700, verification: 120, push: 40 },
      },
      {
        at: ago(1300),
        kind: "gate",
        title: "Code review asked for changes",
        status: "blocked",
        seconds: 600,
        findings,
        other_findings: 3,
      },
    ],
  };
}

const identityDefinition = {
  definition_type: "runtime_settings",
  definition_version: "1.0.0",
};

export function runtimeSettings(): RuntimeSettingsGet {
  return {
    status: "ok",
    definition: { ...identityDefinition },
    draft: { id: "draft-rs-1", revision: "rev00001aaaa", definition: { ...identityDefinition } },
    active_revision: "rev00000ffff",
    yaml: {},
    merged: {
      ...identityDefinition,
      identity: { github_logins: { "lead@bookshelf.example": "bookshelf-lead" } },
    },
    sources: { "identity.github_logins": "inherited" },
    redacted_yaml_draft: "",
    redacted_yaml_live: "",
  };
}
