import { useState } from "react";
import { Button } from "../../components/Button";

export function AdvancedYaml({
  draftYaml,
  liveYaml,
}: {
  draftYaml: string;
  liveYaml: string;
}) {
  const [mode, setMode] = useState<"draft" | "live">("draft");
  const value = mode === "draft" ? draftYaml : liveYaml;

  return (
    <div>
      <h3>Advanced</h3>
      <section>
      <div className="settings-actions">
        <Button
          variant={mode === "draft" ? "primary" : "secondary"}
          onClick={() => setMode("draft")}
        >
          Draft merge
        </Button>
        <Button
          variant={mode === "live" ? "primary" : "secondary"}
          onClick={() => setMode("live")}
        >
          Active live
        </Button>
        <Button
          variant="secondary"
          onClick={() => void navigator.clipboard?.writeText(value)}
        >
          Copy redacted YAML
        </Button>
      </div>
      <label className="form-field">
        <span>{mode === "draft" ? "Draft merge" : "Active live"}</span>
        <textarea aria-label="Redacted YAML" readOnly rows={16} value={value} />
      </label>
      </section>
    </div>
  );
}
