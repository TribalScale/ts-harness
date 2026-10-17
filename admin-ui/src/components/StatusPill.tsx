import { Badge, type BadgeVariant } from "@astryxdesign/core/Badge";
import type { HealthStatus } from "../api/types";

export type SemanticStatus = HealthStatus | "neutral" | "warning" | "success" | "danger" | "muted";

const LABELS: Record<SemanticStatus, string> = {
  ok: "Healthy",
  degraded: "Degraded",
  unavailable: "Unavailable",
  not_configured: "Not configured",
  neutral: "Running",
  warning: "Waiting",
  success: "Succeeded",
  danger: "Failed",
  muted: "Cancelled",
};

const VARIANTS: Record<SemanticStatus, BadgeVariant> = {
  ok: "success",
  degraded: "warning",
  unavailable: "error",
  not_configured: "info",
  neutral: "cyan",
  warning: "orange",
  success: "green",
  danger: "red",
  muted: "neutral",
};

export function StatusPill({ status, label }: { status: SemanticStatus; label?: string }) {
  return <Badge className={`status-pill status-${status}`} variant={VARIANTS[status]} label={label || LABELS[status]} />;
}
