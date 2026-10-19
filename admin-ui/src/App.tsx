import React, { useCallback, useEffect, useRef, useState } from "react";
import type { InboxType } from "./api/types";
import { apiFetch } from "./api/client";
import type { AuthConfig } from "./api/types";
import { LoginPage } from "./auth/LoginPage";
import { setPermissions, setRoles, takeLegacyToken } from "./auth/session";
import { setUnauthorizedHandler } from "./api/client";
import { ChatPage } from "./chat/ChatPage";
import { InboxPage } from "./inbox/InboxPage";
import { AppShell, type NavKey } from "./layout/AppShell";
import { ProgramDefinitionStudio } from "./program-definitions/ProgramDefinitionStudio";
import type { ProgramRoute, StudioTab } from "./program-definitions/types";
import { ProgramsPage } from "./programs/ProgramsPage";
import { SystemPage } from "./system/SystemPage";
import type { FragmentRoute, WorkflowRoute } from "./workflows/types";

interface RouteState {
  nav: NavKey | "login";
  inboxType?: InboxType;
  inboxId?: string;
  sessionId?: string;
  prefill?: string;
  programId?: string;
  systemSection?: string;
  settingsSection?:
    | "defaults"
    | "identity"
    | "routing"
    | "policy"
    | "sandbox"
    | "channels"
    | "toolsets"
    | "connector"
    | "advanced";
  mcpName?: string;
  agentId?: string;
  skillId?: string;
  attachSkill?: string;
  attachStage?: string;
  attachJob?: string;
  attachProgram?: string;
  studioTab?: StudioTab;
  programRoute?: ProgramRoute;
  workflowRoute?: WorkflowRoute;
  fragmentRoute?: FragmentRoute;
}

function parseFragmentRoute(parts: string[]): FragmentRoute {
  if (parts[0] === "new") return { kind: "new" };
  if (parts[0] === "drafts" && parts[1]) return { kind: "draft", draftId: decodeURIComponent(parts[1]) };
  if (parts[0]) return { kind: "fragment", fragmentId: decodeURIComponent(parts[0]) };
  return { kind: "library" };
}

export function fragmentHash(route: FragmentRoute): string {
  if (route.kind === "new") return "#/workflows/fragments/new";
  if (route.kind === "draft") return `#/workflows/fragments/drafts/${encodeURIComponent(route.draftId)}`;
  if (route.kind === "fragment") return `#/workflows/fragments/${encodeURIComponent(route.fragmentId)}`;
  return "#/workflows/fragments";
}

export function parseHash(hash = window.location.hash): RouteState {
  const raw = hash.replace(/^#\/?/, "");
  const [path, queryString = ""] = raw.split("?");
  const query = new URLSearchParams(queryString);
  const attachSkill = query.get("attachSkill") || undefined;
  const attachStage = query.get("stage") || undefined;
  const attachJob = query.get("job") || undefined;
  const attachProgram = query.get("attachProgram") || undefined;
  const parts = path.split("/").filter(Boolean);
  if (!parts.length || parts[0] === "inbox") {
    return {
      nav: "inbox",
      inboxType: parts[1] as InboxType | undefined,
      inboxId: parts[2] ? decodeURIComponent(parts[2]) : undefined,
    };
  }
  if (parts[0] === "chat") {
    const prefill = query.get("prefill") || undefined;
    return { nav: "chat", sessionId: parts[1], prefill };
  }
  if (parts[0] === "programs") {
    return { nav: "programs", programId: parts[1] };
  }
  const attach = { attachSkill, attachStage, attachJob, attachProgram };
  if (parts[0] === "workflows") {
    if (parts[1] === "fragments") {
      return {
        nav: "workflows",
        studioTab: "fragments",
        programRoute: { kind: "library" },
        workflowRoute: { kind: "library" },
        fragmentRoute: parseFragmentRoute(parts.slice(2)),
      };
    }
    if (parts[1] === "programs") {
      if (parts[2] === "new") {
        return { nav: "workflows", studioTab: "programs", programRoute: { kind: "new" }, workflowRoute: { kind: "library" } };
      }
      if (parts[2] === "build") {
        return {
          nav: "workflows",
          studioTab: "programs",
          programRoute: { kind: "build", sessionId: parts[3] ? decodeURIComponent(parts[3]) : undefined },
          workflowRoute: { kind: "library" },
        };
      }
      if (parts[2] === "drafts" && parts[3]) {
        return {
          nav: "workflows",
          studioTab: "programs",
          programRoute: { kind: "draft", draftId: decodeURIComponent(parts[3]) },
          workflowRoute: { kind: "library" },
        };
      }
      if (parts[2]) {
        return {
          nav: "workflows",
          studioTab: "programs",
          programRoute: { kind: "program", programType: decodeURIComponent(parts[2]) },
          workflowRoute: { kind: "library" },
        };
      }
      return {
        nav: "workflows",
        studioTab: "programs",
        programRoute: { kind: "library" },
        workflowRoute: { kind: "library" },
      };
    }
    if (parts[1] === "new") {
      return {
        nav: "workflows",
        studioTab: "workflows",
        programRoute: { kind: "library" },
        workflowRoute: { kind: "new" },
      };
    }
    if (parts[1] === "drafts" && parts[2]) {
      return {
        nav: "workflows",
        studioTab: "workflows",
        programRoute: { kind: "library" },
        workflowRoute: { kind: "draft", draftId: decodeURIComponent(parts[2]) },
      };
    }
    if (parts[1]) {
      return {
        nav: "workflows",
        studioTab: "workflows",
        programRoute: { kind: "library" },
        workflowRoute: { kind: "workflow", workflowType: decodeURIComponent(parts[1]) },
        ...attach,
      };
    }
    return {
      nav: "workflows",
      studioTab: "workflows",
      programRoute: { kind: "library" },
      workflowRoute: { kind: "library" },
    };
  }
  if (parts[0] === "system") {
    const section = parts[1];
    const settingsSection =
      section === "settings"
        ? ([
            "identity",
            "routing",
            "policy",
            "sandbox",
            "channels",
            "toolsets",
            "connector",
            "advanced",
          ].includes(parts[2] || "")
            ? (parts[2] as
                | "identity"
                | "routing"
                | "policy"
                | "sandbox"
                | "channels"
                | "toolsets"
                | "connector"
                | "advanced")
            : "defaults")
        : undefined;
    const rest = parts.slice(2).join("/");
    return {
      nav: "system",
      systemSection: section,
      settingsSection,
      mcpName: section === "mcp" && rest ? decodeURIComponent(rest) : undefined,
      agentId: section === "agents" && rest ? decodeURIComponent(rest) : undefined,
      skillId: section === "skills" && rest ? decodeURIComponent(rest) : undefined,
      ...attach,
    };
  }
  if (parts[0] === "login") return { nav: "login" };
  return { nav: "inbox" };
}

export default function App() {
  const [authed, setAuthed] = useState(false);
  const [, setPermissionStamp] = useState(0);
  const [route, setRoute] = useState<RouteState>(() => parseHash());
  const [workflowDirty, setWorkflowDirty] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [mcpDirty, setMcpDirty] = useState(false);
  const [agentsDirty, setAgentsDirty] = useState(false);
  const previousHashRef = useRef(window.location.hash);
  const restoringHashRef = useRef(false);
  const allowDirtyNavigationRef = useRef(false);
  const settingsDirtyRef = useRef(settingsDirty);
  const mcpDirtyRef = useRef(mcpDirty);
  const agentsDirtyRef = useRef(agentsDirty);
  const workflowDirtyRef = useRef(workflowDirty);
  settingsDirtyRef.current = settingsDirty;
  mcpDirtyRef.current = mcpDirty;
  agentsDirtyRef.current = agentsDirty;
  workflowDirtyRef.current = workflowDirty;

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuthed(false);
      if (!window.location.hash.startsWith("#/login")) {
        window.location.hash = "#/login";
      }
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const legacy = takeLegacyToken();
      if (legacy) {
        try {
          await apiFetch("/admin/auth/session", {
            method: "POST",
            body: JSON.stringify({ access_token: legacy }),
          });
        } catch {
          /* an old token that the gateway rejects just drops the user at login */
        }
      }
      try {
        const cfg = await apiFetch<AuthConfig>("/admin/auth/config");
        if (cancelled) return;
        setPermissions(cfg.permissions || []);
        if (cfg.roles) setRoles(cfg.roles);
        setAuthed(Boolean(cfg.authenticated));
        setPermissionStamp((value) => value + 1);
      } catch {
        if (cancelled) return;
        setPermissions([]);
        setAuthed(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!authed) {
      setPermissions([]);
      return;
    }
    let cancelled = false;
    apiFetch<AuthConfig>("/admin/auth/config")
      .then((cfg) => {
        if (cancelled) return;
        setPermissions(cfg.permissions || []);
        setPermissionStamp((value) => value + 1);
      })
      .catch(() => {
        if (cancelled) return;
        setPermissions([]);
        setPermissionStamp((value) => value + 1);
      });
    return () => {
      cancelled = true;
    };
  }, [authed]);

  useEffect(() => {
    const onHash = () => {
      if (restoringHashRef.current) {
        restoringHashRef.current = false;
        previousHashRef.current = window.location.hash;
        setRoute(parseHash());
        return;
      }
      const previousHash = previousHashRef.current;
      const nextHash = window.location.hash;
      const previousRoute = parseHash(previousHash);
      const nextRoute = parseHash();
      const allowDirtyNavigation = allowDirtyNavigationRef.current;
      allowDirtyNavigationRef.current = false;
      const settingsDirty = settingsDirtyRef.current;
      const mcpDirty = mcpDirtyRef.current;
      const workflowDirty = workflowDirtyRef.current;
      if (
        settingsDirty &&
        previousHash !== nextHash &&
        previousRoute.nav === "system" &&
        previousRoute.systemSection === "settings" &&
        (nextRoute.nav !== "system" || nextRoute.systemSection !== "settings") &&
        !allowDirtyNavigation
      ) {
        if (!window.confirm("Discard unsaved settings changes?")) {
          restoringHashRef.current = true;
          history.replaceState(null, "", previousHash || "#/");
          queueMicrotask(() => {
            restoringHashRef.current = false;
          });
          return;
        }
      }
      if (
        agentsDirtyRef.current &&
        previousHash !== nextHash &&
        previousRoute.nav === "system" &&
        previousRoute.systemSection === "agents" &&
        (nextRoute.nav !== "system" || nextRoute.systemSection !== "agents") &&
        !allowDirtyNavigation
      ) {
        if (!window.confirm("Discard unsaved agent changes?")) {
          restoringHashRef.current = true;
          history.replaceState(null, "", previousHash || "#/");
          queueMicrotask(() => {
            restoringHashRef.current = false;
          });
          return;
        }
      }
      if (
        mcpDirty &&
        previousHash !== nextHash &&
        previousRoute.nav === "system" &&
        previousRoute.systemSection === "mcp" &&
        (nextRoute.nav !== "system" || nextRoute.systemSection !== "mcp") &&
        !allowDirtyNavigation
      ) {
        if (!window.confirm("Discard unsaved MCP changes?")) {
          restoringHashRef.current = true;
          history.replaceState(null, "", previousHash || "#/");
          queueMicrotask(() => {
            restoringHashRef.current = false;
          });
          return;
        }
      }
      if (
        workflowDirty &&
        previousHash !== nextHash &&
        previousRoute.nav === "workflows" &&
        !allowDirtyNavigation
      ) {
        if (!window.confirm("Discard unsaved workflow changes?")) {
          restoringHashRef.current = true;
          history.replaceState(null, "", previousHash || "#/");
          queueMicrotask(() => {
            restoringHashRef.current = false;
          });
          return;
        }
      }
      previousHashRef.current = nextHash;
      setRoute(nextRoute);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [workflowDirty, settingsDirty, mcpDirty, agentsDirty]);

  const setHash = useCallback((hash: string) => {
    window.location.hash = hash;
  }, []);

  const allowDirtyNavigation = useCallback(() => {
    allowDirtyNavigationRef.current = true;
  }, []);

  const navigate = useCallback(
    (nav: NavKey) => {
      if (nav === "workflows") {
        setHash("#/workflows/programs");
        return;
      }
      setHash(`#/${nav}`);
    },
    [setHash]
  );

  useEffect(() => {
    if (!authed || route.nav !== "login") return;
    const next = window.location.pathname + "#/inbox";
    if (window.location.hash !== "#/inbox") {
      window.history.replaceState({}, "", next);
    }
    setRoute(parseHash("#/inbox"));
  }, [authed, route.nav]);

  if (!authed || route.nav === "login") {
    return (
      <LoginPage
        onAuthed={() => {
          // LoginPage moves to #/inbox with replaceState, which fires no hashchange.
          setAuthed(true);
          setRoute(parseHash(window.location.hash));
        }}
      />
    );
  }

  return (
    <AppShell active={route.nav} onNavigate={navigate}>
      {route.nav === "inbox" && (
        <InboxPage
          selectedType={route.inboxType}
          selectedId={route.inboxId}
          onSelect={(type, id) => {
            if (!id) {
              setHash("#/inbox");
              return;
            }
            setHash(`#/inbox/${type}/${encodeURIComponent(id)}`);
          }}
        />
      )}
      {route.nav === "chat" && (
        <ChatPage
          sessionId={route.sessionId}
          prefill={route.prefill}
          onSelectSession={(id) => {
            setHash(id ? `#/chat/${id}` : "#/chat");
          }}
        />
      )}
      {route.nav === "programs" && (
        <ProgramsPage
          programId={route.programId}
          onSelect={(id) => {
            setHash(id ? `#/programs/${id}` : "#/programs");
          }}
        />
      )}
      {route.nav === "workflows" && (
        <ProgramDefinitionStudio
          tab={route.studioTab || "programs"}
          programRoute={route.programRoute || { kind: "library" }}
          workflowRoute={route.workflowRoute || { kind: "library" }}
          fragmentRoute={route.fragmentRoute || { kind: "library" }}
          onNavigateFragment={(next) => setHash(fragmentHash(next))}
          onDirtyChange={setWorkflowDirty}
          allowDirtyNavigation={allowDirtyNavigation}
          onNavigateProgram={(next) => {
            if (next.kind === "library") setHash("#/workflows/programs");
            if (next.kind === "new") setHash("#/workflows/programs/new");
            if (next.kind === "build") {
              setHash(
                next.sessionId
                  ? `#/workflows/programs/build/${encodeURIComponent(next.sessionId)}`
                  : "#/workflows/programs/build"
              );
            }
            if (next.kind === "draft") {
              setHash(`#/workflows/programs/drafts/${encodeURIComponent(next.draftId)}`);
            }
            if (next.kind === "program") {
              setHash(`#/workflows/programs/${encodeURIComponent(next.programType)}`);
            }
          }}
          onNavigateWorkflow={(next) => {
            if (next.kind === "library") setHash("#/workflows");
            if (next.kind === "new") setHash("#/workflows/new");
            if (next.kind === "draft") {
              setHash(`#/workflows/drafts/${encodeURIComponent(next.draftId)}`);
            }
            if (next.kind === "workflow") {
              setHash(`#/workflows/${encodeURIComponent(next.workflowType)}`);
            }
          }}
          onChangeTab={(nextTab) => {
            if (nextTab === "programs") setHash("#/workflows/programs");
            else if (nextTab === "fragments") setHash("#/workflows/fragments");
            else setHash("#/workflows");
          }}
          attachSkill={route.attachSkill}
          attachStage={route.attachStage}
        />
      )}
      {route.nav === "system" && (
        <SystemPage
          section={route.systemSection}
          settingsSection={route.settingsSection}
          mcpName={route.mcpName}
          agentId={route.agentId}
          skillId={route.skillId}
          attachSkill={route.attachSkill}
          attachJob={route.attachJob}
          attachProgram={route.attachProgram}
          onSettingsDirtyChange={setSettingsDirty}
          onMcpDirtyChange={setMcpDirty}
          onAgentsDirtyChange={setAgentsDirty}
        />
      )}
    </AppShell>
  );
}
