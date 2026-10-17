import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { Button } from "../components/Button";
import { Tabs, tabButtonId, tabPanelId } from "../components/Tabs";
import { FragmentBuilderPage } from "../fragments/FragmentBuilderPage";
import { WorkflowBuilderPage } from "../workflows/WorkflowBuilderPage";
import type { FragmentRoute, WorkflowRoute } from "../workflows/types";
import { ProgramBuilderAgentPage } from "./ProgramBuilderAgentPage";
import { ProgramDefinitionBuilderPage } from "./ProgramDefinitionBuilderPage";
import { programsReferencingWorkflow } from "./programGraph";
import type {
  ProgramDefinition,
  ProgramDefinitionSummary,
  ProgramRoute,
  StudioTab,
} from "./types";

const TABS_ID = "definition-studio";

export function ProgramDefinitionStudio({
  tab,
  programRoute,
  workflowRoute,
  fragmentRoute = { kind: "library" },
  onDirtyChange,
  allowDirtyNavigation,
  onNavigateProgram,
  onNavigateWorkflow,
  onNavigateFragment,
  onChangeTab,
  attachSkill,
  attachStage,
}: {
  tab: StudioTab;
  programRoute: ProgramRoute;
  workflowRoute: WorkflowRoute;
  fragmentRoute?: FragmentRoute;
  onDirtyChange?: (dirty: boolean) => void;
  allowDirtyNavigation?: () => void;
  onNavigateProgram: (route: ProgramRoute) => void;
  onNavigateWorkflow: (route: WorkflowRoute) => void;
  onNavigateFragment?: (route: FragmentRoute) => void;
  onChangeTab: (tab: StudioTab) => void;
  attachSkill?: string;
  attachStage?: string;
}) {
  const [programIndex, setProgramIndex] = useState<
    Array<{ program_type: string; display_name?: string; definition?: ProgramDefinition }>
  >([]);

  useEffect(() => {
    let cancelled = false;
    async function loadIndex() {
      try {
        const list = await apiFetch<{ definitions: ProgramDefinitionSummary[] }>(
          "/admin/program-definitions"
        );
        const summaries = list.definitions || [];
        const detailed = await Promise.all(
          summaries
            .filter((item) => item.status === "built_in" || item.status === "active")
            .map(async (item) => {
              try {
                const data = await apiFetch<{ definition: ProgramDefinition }>(
                  `/admin/program-definitions/${encodeURIComponent(item.program_type)}`
                );
                return {
                  program_type: item.program_type,
                  display_name: item.display_name,
                  definition: data.definition,
                };
              } catch {
                return {
                  program_type: item.program_type,
                  display_name: item.display_name,
                };
              }
            })
        );
        if (!cancelled) setProgramIndex(detailed);
      } catch {
        if (!cancelled) setProgramIndex([]);
      }
    }
    void loadIndex();
    return () => {
      cancelled = true;
    };
  }, []);

  const referencingPrograms = useMemo(() => {
    if (workflowRoute.kind !== "workflow") return [];
    return programsReferencingWorkflow(programIndex, workflowRoute.workflowType);
  }, [programIndex, workflowRoute]);

  const openWorkflow = useCallback(
    (workflowType: string) => {
      onNavigateWorkflow({ kind: "workflow", workflowType });
    },
    [onNavigateWorkflow]
  );

  return (
    <div className="definition-studio">
      <div className="definition-studio-tabs panel-pad">
        <Tabs
          idBase={TABS_ID}
          ariaLabel="Definition studio"
          active={tab}
          onChange={(id) => onChangeTab(id as StudioTab)}
          tabs={[
            { id: "programs", label: "Program definitions" },
            { id: "workflows", label: "Workflow definitions" },
            { id: "fragments", label: "Fragments" },
          ]}
        />
      </div>
      <div
        role="tabpanel"
        id={tabPanelId(TABS_ID, tab)}
        aria-labelledby={tabButtonId(TABS_ID, tab)}
        tabIndex={0}
        className="definition-studio-panel"
      >
        {tab === "programs" && programRoute.kind === "build" ? (
          <ProgramBuilderAgentPage sessionId={programRoute.sessionId} onNavigate={onNavigateProgram} />
        ) : tab === "programs" ? (
          <ProgramDefinitionBuilderPage
            route={programRoute}
            onDirtyChange={onDirtyChange}
            allowDirtyNavigation={allowDirtyNavigation}
            onNavigate={onNavigateProgram}
            onOpenWorkflow={openWorkflow}
          />
        ) : tab === "fragments" ? (
          <FragmentBuilderPage
            route={fragmentRoute}
            onDirtyChange={onDirtyChange}
            allowDirtyNavigation={allowDirtyNavigation}
            onNavigate={(next) => onNavigateFragment?.(next)}
          />
        ) : (
          <div className="definition-studio-workflows">
            {workflowRoute.kind === "workflow" && referencingPrograms.length > 0 && (
              <nav className="program-breadcrumb" aria-label="Program definition breadcrumb">
                <span className="muted">Referenced by</span>
                {referencingPrograms.map((item) => (
                  <Button
                    key={item.program_type}
                    variant="ghost"
                    onClick={() =>
                      onNavigateProgram({ kind: "program", programType: item.program_type })
                    }
                  >
                    {item.display_name}
                  </Button>
                ))}
              </nav>
            )}
            <WorkflowBuilderPage
              route={workflowRoute}
              onDirtyChange={onDirtyChange}
              allowDirtyNavigation={allowDirtyNavigation}
              onNavigate={onNavigateWorkflow}
              attachSkill={attachSkill}
              attachStage={attachStage}
            />
          </div>
        )}
      </div>
    </div>
  );
}
