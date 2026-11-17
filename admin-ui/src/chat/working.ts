/** Statuses that still mean the system is working, not waiting or finished. */
export const WORKING_ACTIVITY = ["running", "pending", "revising"] as const;

export function isWorkingActivity(status: string | null | undefined): boolean {
  return (WORKING_ACTIVITY as readonly string[]).includes(String(status || "").trim().toLowerCase());
}
