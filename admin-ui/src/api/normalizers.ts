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
    components: Object.entries(report.components || {}).map(([name, component]) => ({
      name,
      label: HEALTH_LABELS[name] || titleFromKey(name),
      ...component,
    })),
  };
}

export function chatReplyText(response: SessionMessageResponse): string {
  const output = response.output;
  if (typeof output === "object" && output !== null && "summary" in output) {
    const summary = output.summary;
    if (typeof summary === "string") return summary;
    if (typeof summary === "number" || typeof summary === "boolean") return String(summary);
  }
  if (typeof output === "string") return output;
  const fallback = response.reply || response.content || response.message;
  return typeof fallback === "string" ? fallback : "";
}

export function messageDisplayText(message: ChatMessage): string {
  if (message.role === "tool" || message.role === "system") return "";
  if (typeof message.content === "string" && message.content.trim()) return message.content;
  if (message.text?.trim()) return message.text;
  return "";
}

export function artifactRows(artifacts: Record<string, unknown>): ArtifactRow[] {
  return Object.entries(artifacts).map(([key, raw]) => {
    const value = typeof raw === "string" ? raw : JSON.stringify(raw) ?? String(raw);
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
