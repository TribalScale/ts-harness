import { Button } from "../components/Button";
import type { ValidationIssue } from "./types";

export function isActionableProgramValidationPath(path: string, nodeCount?: number): boolean {
  const match = path.match(/^\/nodes\/(\d+)(?:\/|$)/) || path.match(/^\$\.nodes\[(\d+)\](?:\.|$)/);
  if (!match) return false;
  return nodeCount === undefined || Number(match[1]) < nodeCount;
}

export function ProgramValidationPanel({
  issues,
  valid,
  onFocusPath,
  canFocusPath = isActionableProgramValidationPath,
}: {
  issues: ValidationIssue[];
  valid?: boolean;
  onFocusPath: (path: string) => void;
  canFocusPath?: (path: string) => boolean;
}) {
  if (valid && !issues.length) {
    return (
      <section className="validation-panel validation-panel-ok" aria-label="Validation results">
        <strong>Definition is valid.</strong>
      </section>
    );
  }
  if (!issues.length) return null;
  const errorCount = issues.filter((issue) => issue.severity === "error").length;
  const warningCount = issues.filter((issue) => issue.severity === "warning").length;
  return (
    <section
      className={`validation-panel ${valid ? "validation-panel-ok" : ""}`}
      aria-label="Validation results"
    >
      <h3>
        {valid
          ? `Definition is valid${warningCount ? ` with ${warningCount} warning${warningCount === 1 ? "" : "s"}` : ""}.`
          : `Validation issues${errorCount ? ` (${errorCount} error${errorCount === 1 ? "" : "s"})` : ""}`}
      </h3>
      <ul>
        {issues.map((issue, index) => (
          <li key={`${issue.code}:${issue.path}:${index}`} className={`validation-${issue.severity}`}>
            <div>
              <strong>
                {issue.severity === "warning" ? "warning · " : ""}
                {issue.code}
              </strong>
              <p>{issue.message}</p>
              <span className="mono">{issue.path}</span>
            </div>
            {canFocusPath(issue.path) && (
              <Button variant="ghost" onClick={() => onFocusPath(issue.path)}>
                Focus
              </Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
