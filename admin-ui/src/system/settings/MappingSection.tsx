import { useState } from "react";
import { Button } from "../../components/Button";
import { TextInput } from "@astryxdesign/core/TextInput";

export type MappingValue = string | string[];

function display(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join(", ") : String(value ?? "");
}

/** Split "a, b c" into ["a", "b", "c"]; commas or spaces separate items. */
export function splitItems(value: string): string[] {
  return value
    .split(/[,\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * One identity mapping as rows, with an add row and Remove on overlay rows: the same editor
 * for every map (Slack, Jira, GitHub, roles, departments...), so nobody edits JSON by hand.
 * Adding a key that is already there replaces its value.
 */
export function MappingSection({
  title,
  keyLabel,
  valueLabel,
  keyPlaceholder,
  valuePlaceholder,
  hint,
  values,
  overlay,
  list = false,
  lowerKey = false,
  lowerValue = false,
  onSet,
  onReset,
}: {
  title: string;
  keyLabel: string;
  valueLabel: string;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  hint?: string;
  values: Record<string, MappingValue>;
  overlay: boolean;
  /** Values are lists, typed as comma-separated items. */
  list?: boolean;
  lowerKey?: boolean;
  lowerValue?: boolean;
  onSet: (next: Record<string, MappingValue>) => void;
  onReset: () => void;
}) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  function add() {
    const k = lowerKey ? key.trim().toLowerCase() : key.trim();
    const raw = value.trim();
    if (!k || !raw) {
      setError(`Enter both ${keyLabel.toLowerCase()} and ${valueLabel.toLowerCase()}.`);
      return;
    }
    const v: MappingValue = list
      ? splitItems(lowerValue ? raw.toLowerCase() : raw)
      : lowerValue
        ? raw.toLowerCase()
        : raw;
    onSet({ ...values, [k]: v });
    setKey("");
    setValue("");
    setError(null);
  }

  return (
    <section>
      <div className="system-header">
        <h4>{title}</h4>
        <Button variant="secondary" size="sm" type="button" onClick={onReset}>
          Reset to inherited
        </Button>
      </div>
      <p className="muted">Source: {overlay ? "overlay" : "inherited"}</p>
      {hint && <p className="muted">{hint}</p>}
      <table>
        <thead>
          <tr>
            <th>{keyLabel}</th>
            <th>{valueLabel}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(values).map(([k, v]) => (
            <tr key={k}>
              <td>{k}</td>
              <td>{display(v)}</td>
              <td>
                {overlay && (
                  <Button
                    variant="ghost"
                    onClick={() => {
                      const next = { ...values };
                      delete next[k];
                      onSet(next);
                    }}
                  >
                    Remove
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="form-pair">
        <div className="form-field">
          <TextInput label={keyLabel} placeholder={keyPlaceholder} value={key} onChange={(next) => setKey(next)} />
        </div>
        <div className="form-field">
          <TextInput
            label={valueLabel}
            placeholder={valuePlaceholder}
            value={value}
            onChange={(next) => setValue(next)}
          />
        </div>
        <Button variant="secondary" onClick={add}>
          Add
        </Button>
      </div>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

/** A plain list of strings (service permissions) with add and remove. */
export function ListSection({
  title,
  itemLabel,
  placeholder,
  hint,
  items,
  overlay,
  onSet,
  onReset,
}: {
  title: string;
  itemLabel: string;
  placeholder?: string;
  hint?: string;
  items: string[];
  overlay: boolean;
  onSet: (next: string[]) => void;
  onReset: () => void;
}) {
  const [value, setValue] = useState("");
  return (
    <section>
      <div className="system-header">
        <h4>{title}</h4>
        <Button variant="secondary" size="sm" type="button" onClick={onReset}>
          Reset to inherited
        </Button>
      </div>
      <p className="muted">Source: {overlay ? "overlay" : "inherited"}</p>
      {hint && <p className="muted">{hint}</p>}
      <table>
        <tbody>
          {items.map((item) => (
            <tr key={item}>
              <td>{item}</td>
              <td>
                {overlay && (
                  <Button variant="ghost" onClick={() => onSet(items.filter((x) => x !== item))}>
                    Remove
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="form-pair">
        <div className="form-field">
          <TextInput label={itemLabel} placeholder={placeholder} value={value} onChange={(next) => setValue(next)} />
        </div>
        <Button
          variant="secondary"
          onClick={() => {
            const added = splitItems(value.toLowerCase()).filter((item) => !items.includes(item));
            if (added.length) onSet([...items, ...added]);
            setValue("");
          }}
        >
          Add
        </Button>
      </div>
    </section>
  );
}
