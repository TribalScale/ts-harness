import { Button } from "./Button";

export const DEFAULT_PAGE_SIZE = 10;

export function pageCount(total: number, pageSize = DEFAULT_PAGE_SIZE): number {
  return Math.max(1, Math.ceil(Math.max(0, total) / pageSize));
}

export function pageForIndex(index: number, pageSize = DEFAULT_PAGE_SIZE): number {
  if (index < 0) return 1;
  return Math.floor(index / pageSize) + 1;
}

export function paginate<T>(items: T[], page: number, pageSize = DEFAULT_PAGE_SIZE) {
  const total = items.length;
  const pages = pageCount(total, pageSize);
  const current = Math.min(Math.max(1, page), pages);
  const startIndex = (current - 1) * pageSize;
  return {
    items: items.slice(startIndex, startIndex + pageSize),
    page: current,
    pageCount: pages,
    total,
    start: total === 0 ? 0 : startIndex + 1,
    end: Math.min(startIndex + pageSize, total),
  };
}

export function Pagination({
  page,
  pageCount: pages,
  total,
  start,
  end,
  onPageChange,
  itemLabel = "items",
}: {
  page: number;
  pageCount: number;
  total: number;
  start: number;
  end: number;
  onPageChange: (page: number) => void;
  itemLabel?: string;
}) {
  if (pages <= 1) return null;
  return (
    <nav className="pagination" aria-label="Pagination">
      <p className="muted">{`Showing ${start}–${end} of ${total} ${itemLabel}`}</p>
      <div className="pagination-actions">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          Previous
        </Button>
        <span aria-current="page">{`Page ${page} of ${pages}`}</span>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={page >= pages}
          onClick={() => onPageChange(page + 1)}
        >
          Next
        </Button>
      </div>
    </nav>
  );
}
