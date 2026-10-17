import React, { useEffect, useState } from "react";
import { SideNav } from "@astryxdesign/core/SideNav";
import { clearSession, getRoles } from "../auth/session";
import { Button } from "../components/Button";
import { useMediaQuery } from "./useMediaQuery";

export type NavKey = "inbox" | "chat" | "programs" | "workflows" | "system";

const NAV_ITEMS: { key: NavKey; label: string; icon: React.ReactNode }[] = [
  {
    key: "inbox",
    label: "Inbox",
    icon: (
      <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="M4 6h16v12H4z" />
        <path d="M4 14h5l2 2h2l2-2h5" />
      </svg>
    ),
  },
  {
    key: "chat",
    label: "Chat",
    icon: (
      <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="M5 6h14v9H8l-3 3z" />
      </svg>
    ),
  },
  {
    key: "programs",
    label: "Programs",
    icon: (
      <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="M5 5h6v6H5zM13 5h6v6h-6zM5 13h6v6H5zM13 13h6v6h-6z" />
      </svg>
    ),
  },
  {
    key: "workflows",
    label: "Workflows",
    icon: (
      <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="M6 6h5v5H6zM13 13h5v5h-5z" />
        <path d="M11 8.5h3a2 2 0 0 1 2 2V13M13 15.5h-3a2 2 0 0 1-2-2V11" />
      </svg>
    ),
  },
  {
    key: "system",
    label: "System",
    icon: (
      <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="M12 4l7 4v8l-7 4-7-4V8z" />
        <path d="M12 9v6M9 12h6" />
      </svg>
    ),
  },
];

export function AppShell({
  active,
  onNavigate,
  children,
}: {
  active: NavKey;
  onNavigate: (key: NavKey) => void;
  children: React.ReactNode;
}) {
  const roles = getRoles();
  const desktop = useMediaQuery("(min-width: 1024px)");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const signOut = () => {
    clearSession();
    window.location.hash = "#/login";
    window.location.reload();
  };

  useEffect(() => {
    if (desktop) setDrawerOpen(false);
  }, [desktop]);

  useEffect(() => {
    if (!drawerOpen) return;
    const close = () => setDrawerOpen(false);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("hashchange", close);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("hashchange", close);
    };
  }, [drawerOpen]);

  const navigation = (
    <nav className="app-nav" aria-label="Primary">
      {NAV_ITEMS.map(({ key, label, icon }) => (
        <button
          key={key}
          aria-current={active === key ? "page" : undefined}
          className={active === key ? "active" : ""}
          onClick={() => {
            setDrawerOpen(false);
            onNavigate(key);
          }}
        >
          <span aria-hidden="true" className="nav-icon">
            {icon}
          </span>
          <span>{label}</span>
        </button>
      ))}
    </nav>
  );

  const operatorFooter = (
    <div className="operator-footer">
      <span className="operator-role">{roles.length ? roles.join(", ") : "operator"}</span>
      <Button variant="ghost" onClick={signOut}>
        Sign out
      </Button>
    </div>
  );

  const brand = (
    <div className="app-brand">
      <span className="brand-mark">EC</span>
      <span>TS Harness</span>
    </div>
  );

  if (desktop) {
    return (
      <div className="app-shell">
        <SideNav className="app-sidebar" aria-label="Sidebar" header={brand} footer={operatorFooter}>
          {navigation}
        </SideNav>
        <main className="app-main">{children}</main>
      </div>
    );
  }

  return (
    <div className="app-shell app-shell-compact">
      <header className="app-topbar">
        <Button
          variant="ghost"
          className="menu-button"
          aria-expanded={drawerOpen}
          aria-controls="app-nav-drawer"
          onClick={() => setDrawerOpen((open) => !open)}
        >
          Menu
        </Button>
        {brand}
      </header>
      {drawerOpen && (
        <>
          <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)} />
          <aside className="app-sidebar nav-drawer" id="app-nav-drawer">
            {navigation}
            {operatorFooter}
          </aside>
        </>
      )}
      <main className="app-main">{children}</main>
    </div>
  );
}
