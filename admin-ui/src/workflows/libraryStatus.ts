export const LIBRARY_STATUSES = ["draft", "active", "built_in"] as const;

export type LibraryStatus = (typeof LIBRARY_STATUSES)[number];

export const LIBRARY_STATUS_LABELS: Record<LibraryStatus, string> = {
  draft: "Drafts",
  active: "Active custom",
  built_in: "Built-ins",
};

export function defaultLibraryStatus<T extends { status: string }>(
  groups: Record<LibraryStatus, T[]>,
  preferred?: LibraryStatus
): LibraryStatus {
  if (preferred && groups[preferred].length) return preferred;
  return LIBRARY_STATUSES.find((status) => groups[status].length) || "built_in";
}
