import { useRef } from "react";

export interface TabOption {
  id: string;
  label: string;
  count?: number;
}

export function tabButtonId(idBase: string, id: string): string {
  return `${idBase}-tab-${id}`;
}

export function tabPanelId(idBase: string, id: string): string {
  return `${idBase}-panel-${id}`;
}

export function Tabs({
  tabs,
  active,
  onChange,
  ariaLabel,
  idBase,
}: {
  tabs: TabOption[];
  active: string;
  onChange: (id: string) => void;
  ariaLabel: string;
  idBase: string;
}) {
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});

  const selectOffset = (offset: number) => {
    const index = tabs.findIndex((tab) => tab.id === active);
    const next = tabs[(index + offset + tabs.length) % tabs.length];
    onChange(next.id);
    buttons.current[next.id]?.focus();
  };

  return (
    <div className="tabs" role="tablist" aria-label={ariaLabel}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          ref={(node) => {
            buttons.current[tab.id] = node;
          }}
          id={tabButtonId(idBase, tab.id)}
          role="tab"
          aria-selected={active === tab.id}
          aria-controls={tabPanelId(idBase, tab.id)}
          tabIndex={active === tab.id ? 0 : -1}
          className={active === tab.id ? "active" : ""}
          onClick={() => onChange(tab.id)}
          onKeyDown={(event) => {
            if (event.key === "ArrowRight") {
              event.preventDefault();
              selectOffset(1);
            }
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              selectOffset(-1);
            }
          }}
        >
          {tab.label}
          {tab.count === undefined ? "" : ` (${tab.count})`}
        </button>
      ))}
    </div>
  );
}
