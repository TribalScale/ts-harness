import type {
  ArtifactRow,
  ChatMessage,
  HealthReport,
  NormalizedHealthReport,
  SessionMessageResponse,
} from "./types";

const HEALTH_LABELS: Record<string, string> = {
  provider: "Provider",
  memory_api: "Memory API",
  mcp: "MCP",
  google_workspace: "Google Workspace",
  channels: "Channels",
  sandbox: "Sandbox",
  scheduler: "Scheduler",
  slack_socket: "Slack",
};

function titleFromKey(key: string): string {
  return key.split("_").join(" ");
}

export function normalizeHealth(report: HealthReport): NormalizedHealthReport {
  return {
    status: report.status,
    version: report.version,
    components: Object.entries(report.components || {}).map(
      ([name, component]) => ({
        name,
        label: HEALTH_LABELS[name] || titleFromKey(name),
        ...component,
      }),
    ),
  };
}

export function chatReplyText(response: SessionMessageResponse): string {
  const output = response.output;
  if (typeof output === "object" && output !== null && "summary" in output) {
    const summary = output.summary;
    if (typeof summary === "string") return summary;
    if (typeof summary === "number" || typeof summary === "boolean")
      return String(summary);
  }
  if (typeof output === "string") return output;
  const fallback = response.reply || response.content || response.message;
  return typeof fallback === "string" ? fallback : "";
}

export function messageDisplayText(message: ChatMessage): string {
  if (message.role === "tool" || message.role === "system") return "";
  if (typeof message.content === "string" && message.content.trim())
    return message.content;
  if (message.text?.trim()) return message.text;
  return "";
}

export function artifactRows(
  artifacts: Record<string, unknown>,
): ArtifactRow[] {
  return Object.entries(artifacts).map(([key, raw]) => {
    const value =
      typeof raw === "string" ? raw : (JSON.stringify(raw) ?? String(raw));
    const href = /^https?:\/\//.test(value) ? value : undefined;
    return {
      key,
      label: titleFromKey(key),
      value,
      href,
      expandable: value.length > 120,
    };
  });
}

export type ProgramDocument = {
  key: string;
  title: string;
  markdown: string;
  stage: string;
};

const DOCUMENT_TITLES: Record<string, string> = {
  prd_markdown: "PRD",
  frds_markdown: "Feature requirements (FRDs)",
  feature_tree_markdown: "Feature tree",
  requirements_markdown: "Requirements",
  product_overview_markdown: "Product overview",
  blueprints_markdown: "Blueprints",
  architecture_summary_markdown: "Architecture summary",
  architecture_decisions_markdown: "Architecture decisions",
  work_orders_markdown: "Work orders",
  context_graph_markdown: "Context graph",
  execution_phases_markdown: "Execution phases",
};

function docTitle(key: string): string {
  return DOCUMENT_TITLES[key] || titleFromKey(key.replace(/_markdown$/, ""));
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** The PRD and planning documents a program produced, in the order they were written. */
export function programDocuments(
  artifacts: Record<string, unknown>,
): ProgramDocument[] {
  const docs: ProgramDocument[] = [];
  const seen = new Set<string>();
  const add = (stage: string, key: string, value: unknown) => {
    if (typeof value !== "string" || !value.trim() || seen.has(key)) return;
    seen.add(key);
    docs.push({ key, title: docTitle(key), markdown: value, stage });
  };
  const prd = artifacts.approved_prd;
  if (typeof prd === "string") add("PRD", "prd_markdown", prd);
  else add("PRD", "prd_markdown", record(record(prd).result).prd_markdown);
  for (const [stage, name] of [
    ["Refinery", "refinery_artifacts"],
    ["Foundry", "foundry_artifacts"],
    ["Planner", "planner_artifacts"],
  ] as const) {
    for (const [key, value] of Object.entries(record(artifacts[name]))) {
      if (key.endsWith("_markdown")) add(stage, key, value);
    }
  }
  add("Planner", "work_orders_markdown", artifacts.work_orders_markdown);
  return docs;
}
