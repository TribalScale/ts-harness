import { useCallback, useEffect, useId, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import type { BudgetStatus, BudgetsOverview, TeamBudget } from "../api/types";
import { Button } from "../components/Button";
import { PageState } from "../components/PageState";
import { StatusPill } from "../components/StatusPill";

const STATUS: Record<BudgetStatus, { status: "success" | "warning" | "danger"; label: string }> = {
  ok: { status: "success", label: "On track" },
  warning: { status: "warning", label: "Near budget" },
  reached: { status: "danger", label: "Budget reached" },
};

function money(value: number, currency: string) {
  return `${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;
}

function monthLabel(month: string) {
  return new Date(`${month}T00:00:00Z`).toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
}

function RaiseDialog({
  team,
  currency,
  limit,
  onClose,
  onRaised,
}: {
  team: TeamBudget | null;
  currency: string;
  limit: number | null;
  onClose: () => void;
  onRaised: () => Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [amount, setAmount] = useState("");
  const [scope, setScope] = useState<"month" | "permanent">("month");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (team && !dialog.open) {
      setAmount(String(Math.max(team.budget_usd, Math.ceil(team.spent_usd))));
      setScope("month");
      setReason("");
      setError(null);
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!team && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [team]);

  const value = Number(amount);
  const overLimit = limit !== null && value > limit;
  const invalid = amount.trim() === "" || !Number.isFinite(value) || value < 0 || !reason.trim() || overLimit;

  async function submit() {
    if (!team || invalid) return;
    setBusy(true);
    try {
      await apiFetch(`/admin/budgets/${encodeURIComponent(team.id)}/raise`, {
        method: "POST",
        body: JSON.stringify({ amount_usd: value, scope, reason: reason.trim() }),
      });
      await onRaised();
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
      {team && (
        <section aria-label="Raise budget">
          <h2 id={titleId}>Change budget for {team.display_name || team.slug}</h2>
          <p className="muted">
            Spent {money(team.spent_usd, currency)} of {money(team.budget_usd, currency)} this month.
            {limit !== null ? ` Managers can set up to ${money(limit, currency)}.` : ""}
          </p>
          <label className="form-field">
            <span>Monthly budget ({currency})</span>
            <input
              aria-label="Monthly budget"
              type="number"
              min={0}
              step="100"
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
            <span>Applies to</span>
            <select aria-label="Applies to" value={scope} onChange={(e) => setScope(e.target.value as "month" | "permanent")}>
              <option value="month">This month only</option>
              <option value="permanent">Every month from now</option>
            </select>
          </label>
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

export function BudgetsPage() {
  const [data, setData] = useState<BudgetsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [raising, setRaising] = useState<TeamBudget | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await apiFetch<BudgetsOverview>("/admin/budgets"));
      setError(null);
    } catch (e: any) {
      setError(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!data && !error) return <PageState kind="loading" title="Loading budgets" />;
  if (!data) return <PageState kind="error" title="Could not load budgets" detail={error || ""} onRetry={() => void load()} />;

  return (
    <section aria-label="Budgets" className="budgets-page">
      <div className="system-header">
        <div>
          <h3>LLM budgets · {monthLabel(data.month)}</h3>
          <p className="muted">
            Teams get {money(data.default_team_monthly, data.currency)} a month unless set otherwise. At{" "}
            {data.warn_at_percent}% the team is warned; at 100% its agent and coding work waits until the budget is
            raised.
          </p>
        </div>
      </div>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
      {data.teams.length === 0 ? (
        <PageState kind="empty" title="No teams" detail="Teams come from the identity directory or SCIM." />
      ) : (
        <div className="cron-table-wrap">
          <table className="cron-table">
            <thead>
              <tr>
                <th>Team</th>
                <th>Spent</th>
                <th>Budget</th>
                <th>Used</th>
                <th>Status</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {data.teams.map((team) => (
                <tr key={team.id}>
                  <td>
                    {team.display_name || team.slug}
                    {team.overrides[0] && (
                      <div className="muted" title={team.overrides[0].reason}>
                        Set by {team.overrides[0].raised_by} ({team.overrides[0].scope === "month" ? "this month" : "permanent"})
                      </div>
                    )}
                  </td>
                  <td>{money(team.spent_usd, data.currency)}</td>
                  <td>{money(team.budget_usd, data.currency)}</td>
                  <td>
                    <meter
                      aria-label={`${team.slug} budget used`}
                      min={0}
                      max={100}
                      low={data.warn_at_percent}
                      high={99.99}
                      optimum={0}
                      value={Math.min(team.percent, 100)}
                    />{" "}
                    {team.percent}%
                  </td>
                  <td>
                    <StatusPill status={STATUS[team.status].status} label={STATUS[team.status].label} />
                    {team.held_tasks > 0 && <div className="muted">{team.held_tasks} tasks waiting</div>}
                  </td>
                  <td className="actions">
                    {team.can_raise && (
                      <Button variant="secondary" onClick={() => setRaising(team)}>
                        Change budget
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.unassigned_spent_usd ? (
        <p className="muted">
          {money(data.unassigned_spent_usd, data.currency)} this month came from calls without a team (chats from people
          outside any team, programs without a project).
        </p>
      ) : null}
      <RaiseDialog
        team={raising}
        currency={data.currency}
        limit={data.raise_limit}
        onClose={() => setRaising(null)}
        onRaised={load}
      />
    </section>
  );
}
