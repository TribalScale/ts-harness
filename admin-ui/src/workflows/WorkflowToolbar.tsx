import { Button } from "../components/Button";
import type { FragmentSummary } from "./types";

export function WorkflowToolbar({
  readOnly,
  onAddTask,
  onAddGate,
  onAddExpand,
  onAddCompensate,
  onAddWait,
  onAddBarrier,
  onAddChildWorkflows,
  fragmentPicker,
  onToggleFragmentPicker,
  onInsertFragment,
  onDuplicate,
  onDelete,
  onFitView,
}: {
  readOnly: boolean;
  onAddTask: () => void;
  onAddGate: () => void;
  onAddExpand: () => void;
  onAddCompensate: () => void;
  onAddWait: () => void;
  onAddBarrier: () => void;
  onAddChildWorkflows?: () => void;
  fragmentPicker?: { open: boolean; fragments: FragmentSummary[]; error?: string | null };
  onToggleFragmentPicker?: () => void;
  onInsertFragment?: (fragment: FragmentSummary) => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onFitView: () => void;
}) {
  return (
    <div className="workflow-toolbar" role="toolbar" aria-label="Workflow canvas tools">
      <Button disabled={readOnly} variant="secondary" onClick={onAddTask}>
        Add task
      </Button>
      <Button disabled={readOnly} variant="secondary" onClick={onAddGate}>
        Add gate
      </Button>
      <Button disabled={readOnly} variant="secondary" onClick={onAddExpand}>
        Add expand
      </Button>
      <Button disabled={readOnly} variant="secondary" onClick={onAddCompensate}>
        Add compensate
      </Button>
      <Button disabled={readOnly} variant="secondary" onClick={onAddWait}>
        Add wait
      </Button>
      <Button disabled={readOnly} variant="secondary" onClick={onAddBarrier}>
        Add barrier
      </Button>
      {onAddChildWorkflows && (
        <Button disabled={readOnly} variant="secondary" onClick={onAddChildWorkflows}>
          Child workflows
        </Button>
      )}
      {onToggleFragmentPicker && (
        <Button disabled={readOnly} variant="secondary" onClick={onToggleFragmentPicker}>
          Insert fragment
        </Button>
      )}
      <Button disabled={readOnly} variant="secondary" onClick={onDuplicate}>
        Duplicate selected
      </Button>
      <Button disabled={readOnly} variant="danger" onClick={onDelete}>
        Delete selected
      </Button>
      <Button variant="secondary" onClick={onFitView}>
        Fit view
      </Button>
      {fragmentPicker?.open && (
        <div className="workflow-fragment-picker" role="listbox" aria-label="Active fragments">
          {fragmentPicker.error && <span className="error">{fragmentPicker.error}</span>}
          {!fragmentPicker.error && !fragmentPicker.fragments.length && (
            <span className="muted">No active fragments.</span>
          )}
          {fragmentPicker.fragments.map((fragment) => (
            <Button
              key={fragment.fragment_id}
              variant="ghost"
              onClick={() => onInsertFragment?.(fragment)}
            >
              {fragment.display_name || fragment.fragment_id} v{fragment.definition_version}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}
