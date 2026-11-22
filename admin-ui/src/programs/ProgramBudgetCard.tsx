import { useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import type { ProgramBudget, ProgramOwnBudget } from "../api/types";
import { money, STATUS } from "../budgets/BudgetsPage";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";

const ACTION_TEXT: Record<ProgramOwnBudget["action"], string> = {
  downgrade:
    "revisions, complex first runs and pre-push reviews use the default coding model instead of the stronger one",
  stop: "new coding jobs for its tickets wait",
};

function SetBudgetDialog({
  programId,
  budget,
  open,
  onClose,
  onSaved,
}: {
  programId: string;
  budget: ProgramOwnBudget;
  open: boolean;
  onClose: () => void;
  onSaved: (data: ProgramBudget) => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setAmount(budget.source === "program" ? String(budget.budget_usd) : "");
      setReason("");
      setError(null);
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open, budget]);

  const cleared = amount.trim() === "";
  const value = Number(amount);
  const overLimit = !cleared && budget.set_limit !== null && value > budget.set_limit;
  const invalid = (!cleared && (!Number.isFinite(value) || value < 0)) || !reason.trim() || overLimit;

  async function submit() {
    if (invalid) return;
    setBusy(true);
    try {
      const data = await apiFetch<ProgramBudget>(`/admin/budgets/programs/${encodeURIComponent(programId)}`, {
        method: "PUT",
        body: JSON.stringify({ amount_usd: cleared ? null : value, reason: reason.trim() }),
      });
      onSaved(data);
      onClose();
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      className="dialog dialog-form"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      {open && (
        <section aria-label="Set program budget">
          <h2 id={titleId}>Program LLM budget</h2>
          <p className="muted">
            Spent {money(budget.spent_usd, budget.currency)} so far. Leave the amount empty to use the default for every
            program.
            {budget.set_limit !== null ? ` Managers can set up to ${money(budget.set_limit, budget.currency)}.` : ""}
          </p>
          <label className="form-field">
            <span>Budget for the whole program ({budget.currency})</span>
            <input
              aria-label="Program budget"
              type="number"
              min={0}
              step="10"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </label>
          {overLimit && (
            <p className="field-error" role="alert">
              Above the manager limit; ask an admin.
            </p>
          )}
          <label className="form-field">
            <span>Reason</span>
            <textarea aria-label="Reason" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}
          <div className="dialog-actions">
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button busy={busy} disabled={invalid} onClick={() => void submit()}>
              Save budget
            </Button>
          </div>
        </section>
      )}
    </dialog>
  );
}

/** Spend against the program's own LLM budget, what happens at 100%, and a way to change it. */
export function ProgramBudgetCard({
  programId,
  budget,
  onSaved,
}: {
  programId: string;
  budget: ProgramOwnBudget;
  onSaved: (data: ProgramBudget) => void;
}) {
  const [editing, setEditing] = useState(false);
  const { currency } = budget;
  const spent = money(budget.spent_usd, currency);
  const month = money(budget.spent_month_usd, currency);

  return (
    <section className="callout program-budget" aria-label="Program LLM budget">
      <strong>LLM budget</strong>
      {budget.status === "none" ? (
        <p>
          No budget. Spent {spent} ({month} this month).
        </p>
      ) : (
        <>
          <p>
            Spent {spent} of {money(budget.budget_usd, currency)}{" "}
            <meter
              aria-label="Program budget used"
              min={0}
              max={100}
              low={budget.warn_at_percent}
              high={99.99}
              optimum={0}
              value={Math.min(budget.percent ?? 0, 100)}
            />{" "}
            {budget.percent}% <StatusPill status={STATUS[budget.status].status} label={STATUS[budget.status].label} />
          </p>
          <p className="muted">
            {month} this month.{" "}
            {budget.source === "program"
              ? `Set for this program${budget.set_by ? ` by ${budget.set_by}` : ""}.`
              : "Default for every program."}{" "}
            {budget.status === "reached"
              ? `Budget reached: ${ACTION_TEXT[budget.action]}${
                  budget.action === "stop" && budget.held_jobs ? ` (${budget.held_jobs} waiting)` : ""
                }.`
              : `At ${budget.warn_at_percent}% the program's thread is warned; at 100% ${ACTION_TEXT[budget.action]}.`}
          </p>
        </>
      )}
      {budget.can_set && (
        <Button variant="secondary" onClick={() => setEditing(true)}>
          {budget.source === "program" ? "Change budget" : "Set budget"}
        </Button>
      )}
      <SetBudgetDialog
        programId={programId}
        budget={budget}
        open={editing}
        onClose={() => setEditing(false)}
        onSaved={onSaved}
      />
    </section>
  );
}
