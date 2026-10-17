import { Button } from "../components/Button";
import type { ValidationIssue } from "./types";

export function isActionableValidationPath(path: string, stageCount?: number): boolean {
  const match = path.match(/^\/stages\/(\d+)(?:\/|$)/) || path.match(/^\$\.stages\[(\d+)\](?:\.|$)/);
  if (!match) return false;
  return stageCount === undefined || Number(match[1]) < stageCount;
}

export function ValidationPanel({
  issues,
  valid,
  onFocusPath,
  canFocusPath = isActionableValidationPath,
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
  return (
    <section className="validation-panel" aria-label="Validation results">
      <h3>Validation issues</h3>
      <ul>
        {issues.map((issue, index) => (
          <li key={`${issue.code}:${issue.path}:${index}`} className={`validation-${issue.severity}`}>
            <div>
              <strong>{issue.code}</strong>
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
